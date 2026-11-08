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
// not a request, and must not suppress a real prompt.
export function isTriggerComment(body) {
  const lines = String(body || '').split('\n');
  let fenced = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (line.startsWith('```')) { fenced = !fenced; continue; }
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
