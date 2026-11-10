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
// 'forbidden' or 'rate-limited' (on a verdictFor read or a POST) is terminal for the run: workers stop picking up
// new PRs (no further reads), and prompt POSTs are serialized through a shared
// chain that re-checks the terminal flag under the lock, so at most one refused
// write fires. Blocked prompts and unprocessed PRs are counted separately so the
// uncompleted obligations stay explicit for the caller.
export async function sweepPrompts(prs, { verdictFor, postTrigger, concurrency = 4, log = () => {}, logError = () => {} }) {
  let prompted = 0;
  let failed = 0;
  let blocked = 0;
  let processed = 0;
  let terminalClassification = null;
  let next = 0;
  let postChain = Promise.resolve();
  // Any classified forbidden or rate-limited failure, read or write, is terminal.
  const markTerminal = (error) => {
    if (!terminalClassification && (error.classification === 'forbidden' || error.classification === 'rate-limited')) {
      terminalClassification = error.classification;
      logError(`terminal ${error.classification} failure classified; further reads and prompt POSTs are blocked this run`);
    }
  };
  const postSerialized = (pr) => {
    const run = postChain.then(async () => {
      if (terminalClassification) {
        blocked += 1;
        log(`#${pr.number} prompt blocked: terminal ${terminalClassification} failure already classified this run`);
        return;
      }
      try {
        await postTrigger(pr);
        prompted += 1;
      } catch (error) {
        failed += 1;
        logError(`#${pr.number} error: ${error.message}`);
        markTerminal(error);
      }
    });
    postChain = run.catch(() => {});
    return run;
  };
  async function worker() {
    while (next < prs.length) {
      if (terminalClassification) return;
      const pr = prs[next];
      next += 1;
      let verdict;
      try {
        verdict = await verdictFor(pr);
      } catch (error) {
        failed += 1;
        processed += 1;
        logError(`#${pr.number} error: ${error.message}`);
        markTerminal(error);
        continue;
      }
      processed += 1;
      if (verdict !== 'prompt') continue;
      await postSerialized(pr);
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, Math.min(concurrency, prs.length)) }, worker));
  return { prompted, failed, blocked, unprocessed: prs.length - processed, terminalClassification };
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

// Error diagnostics never need more than a few KiB, but a hostile or broken proxy can
// stream an unbounded error body.  Cap the read instead of buffering it whole.
export const ERROR_BODY_CAP = 16 * 1024;

export async function readBoundedBody(res, max = ERROR_BODY_CAP) {
  if (!res.body) return '';
  const reader = res.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > max) {
        await reader.cancel().catch(() => {});
        break;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  return new TextDecoder().decode(Buffer.concat(chunks));
}

export function describeApiError({ method = 'GET', path = '', status, headers, bodyText = '' } = {}) {
  const parts = [`${clean(method, 20)} ${clean(path, 200)} -> ${clean(status, 10)}`];
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
