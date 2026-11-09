// Pure helpers for the Kody first-review prompter. No I/O here; prompter.mjs owns GitHub.
//
// What this automates (owner request 2026-11-07, iMessage): request Kody's initial
// review on every open fleet PR that has never had one, instead of stopping repeat
// reviews. Kodus' global review cadence is Manual, so follow-up reviews only happen
// when someone comments @kody start-review; this script posts that comment once per
// unreviewed PR and never again.
//
// What this deliberately does NOT do:
// - Retry a failed review. A "Code Review Could Not Complete" comment from Kody means
//   Kody already answered the request; re-prompting on a schedule would spam failures
//   during model-capacity incidents. Failed PRs stay manual.
// - Guarantee exactly-once across systems. The list-check-post sequence races with
//   Kodus' own automatic initial review and with people or agents commenting by hand.
//   The window is one extra trigger comment, which Kody treats as a no-op re-request
//   on an already-reviewed or already-running PR. No duplicate reviews were observed
//   during the 2026-11-07 manual sweep, but that observation is not a queue-dedup
//   guarantee; the 30-minute age grace below is what keeps the race window small.

export const KODY_LOGIN = 'kody-ai[bot]';
export const TRIGGER_TEXT = '@kody start-review';
export const MIN_PR_AGE_MINUTES = 30;

// A trigger request counts only as its own line outside a fenced code block, matching
// the fleet's kody-defer command parsing: a quoted or fenced mention is discussion,
// not a request, and must not suppress a real prompt. Both ``` and ~~~ open or close a
// fence, the same test as kody-defer's fenceToggle.
function fenceToggle(line) {
  return /^(```|~~~)/.test(line);
}

export function isTriggerComment(body) {
  const lines = String(body || '').split('\n');
  let fenced = false;
  for (const raw of lines) {
    const line = raw.trim().replace(/\r$/, '');
    if (fenceToggle(line)) { fenced = !fenced; continue; }
    if (fenced) continue;
    if (line.toLowerCase() === TRIGGER_TEXT) return true;
  }
  return false;
}

// Kody has answered the PR at all: any issue comment or review from the Kody app.
// "Answered" includes failure comments, so failures are never auto-retried.
export function kodyHasAnswered({ issueComments = [], reviews = [] } = {}) {
  const fromKody = (entry) => entry?.user?.login === KODY_LOGIN;
  return issueComments.some(fromKody) || reviews.some(fromKody);
}

export function hasPendingTrigger(issueComments = []) {
  return issueComments.some((comment) => isTriggerComment(comment?.body));
}

export function oldEnough(pr, now = Date.now()) {
  const created = Date.parse(pr?.created_at);
  if (!Number.isFinite(created)) return false;
  return now - created >= MIN_PR_AGE_MINUTES * 60 * 1000;
}

// One decision per PR. Returns 'prompt' or a skip reason string.
export function decide(pr, context = {}) {
  if (!pr || pr.state !== 'open') return 'not-open';
  if (pr.head?.repo?.full_name?.toLowerCase() !== pr.base?.repo?.full_name?.toLowerCase()) {
    return 'fork-pr';
  }
  if (!oldEnough(pr, context.now)) return 'too-new';
  if (kodyHasAnswered(context)) return 'kody-answered';
  if (hasPendingTrigger(context.issueComments)) return 'trigger-pending';
  return 'prompt';
}

// A bounded, non-secret reason for a failed GitHub API call. The log used to carry
// only the status, so a 403 could not be told apart (permission, ruleset, rate limit).
// Only GitHub's own short message, its public documentation_url and a fixed set of
// non-credential headers are kept. Anything token-shaped is redacted, and the whole
// text is length-capped.
const DIAGNOSTIC_HEADERS = [
  'x-accepted-github-permissions',
  'x-github-request-id',
  'x-ratelimit-remaining',
  'x-ratelimit-resource',
  'retry-after',
];
const TOKEN_SHAPED = /(gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|bearer\s+[A-Za-z0-9._~+/=-]{10,})/gi;
const clean = (value, max) =>
  String(value ?? '')
    .replace(TOKEN_SHAPED, '[redacted]')
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .slice(0, max);

export function describeApiError({ method = 'GET', path = '', status, headers, bodyText = '' } = {}) {
  const parts = [`${method} ${path} -> ${status}`];
  let message = '';
  let docs = '';
  try {
    const parsed = JSON.parse(bodyText);
    if (typeof parsed?.message === 'string') message = parsed.message;
    if (typeof parsed?.documentation_url === 'string') docs = parsed.documentation_url;
  } catch {
    // Non-JSON body: report nothing from it rather than echo arbitrary content.
  }
  if (message) parts.push(`message=${JSON.stringify(clean(message, 200))}`);
  if (docs.startsWith('https://docs.github.com/')) parts.push(`docs=${clean(docs, 200)}`);
  for (const name of DIAGNOSTIC_HEADERS) {
    const value = headers?.get ? headers.get(name) : headers?.[name];
    if (value) parts.push(`${name}=${clean(value, 120)}`);
  }
  return parts.join(' ');
}
