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

// Failure classification for the prompter's GitHub calls. Only the status and a
// fixed allowlist of response headers are ever used or logged: request id,
// rate-limit counters, and retry-after. The response body (which can carry
// private content) and the request (which carries the token) are never read.
export const SAFE_ERROR_HEADERS = [
  'x-github-request-id',
  'x-ratelimit-limit',
  'x-ratelimit-remaining',
  'x-ratelimit-reset',
  'retry-after',
];

// 'rate-limited' only on positive rate-limit evidence; a bare 403/401 is a
// terminal 'forbidden' (policy/authorization), everything else is 'unknown'.
export function classifyHttpError(status, headers = {}) {
  if (status === 429) return 'rate-limited';
  if (status === 403) {
    if (headers['retry-after'] != null || headers['x-ratelimit-remaining'] === '0') {
      return 'rate-limited';
    }
    return 'forbidden';
  }
  if (status === 401) return 'forbidden';
  return 'unknown';
}

// Log-safe one-line failure detail: status, class, allowlisted headers only.
export function safeErrorDetails(status, headers = {}) {
  const parts = [`status=${status}`, `class=${classifyHttpError(status, headers)}`];
  for (const name of SAFE_ERROR_HEADERS) {
    const value = headers[name];
    if (value != null) parts.push(`${name}=${String(value).slice(0, 64)}`);
  }
  return parts.join(' ');
}

// The prompt sweep. verdictFor(pr) returns the decide() verdict (and logs it);
// postTrigger(pr) posts the trigger comment. A post failure classified
// 'forbidden' or 'rate-limited' is terminal for the run: remaining prompt
// verdicts are counted as blocked instead of attempted, and the returned counts
// keep the uncompleted obligations explicit for the caller.
export async function sweepPrompts(prs, { verdictFor, postTrigger, concurrency = 4, log = () => {}, logError = () => {} }) {
  let prompted = 0;
  let failed = 0;
  let blocked = 0;
  let terminalClassification = null;
  let next = 0;
  async function worker() {
    while (next < prs.length) {
      const pr = prs[next];
      next += 1;
      let verdict;
      try {
        verdict = await verdictFor(pr);
      } catch (error) {
        failed += 1;
        logError(`#${pr.number} error: ${error.message}`);
        continue;
      }
      if (verdict !== 'prompt') continue;
      if (terminalClassification) {
        blocked += 1;
        log(`#${pr.number} prompt blocked: terminal ${terminalClassification} failure already classified this run`);
        continue;
      }
      try {
        await postTrigger(pr);
        prompted += 1;
      } catch (error) {
        failed += 1;
        logError(`#${pr.number} error: ${error.message}`);
        if (!terminalClassification && (error.classification === 'forbidden' || error.classification === 'rate-limited')) {
          terminalClassification = error.classification;
          logError(`terminal ${error.classification} failure classified; further prompt POSTs are blocked this run`);
        }
      }
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, Math.min(concurrency, prs.length)) }, worker));
  return { prompted, failed, blocked, terminalClassification };
}
