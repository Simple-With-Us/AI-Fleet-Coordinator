// Decision table for one successful Kody review per pull request (AFC-735).
//
// Pure. No GitHub calls. The live review is started by the kody-ai GitHub App
// from Kodus's hosted review cadence, which this module cannot switch. The
// table is the contract for that setting and for any later gate that posts
// `@kody start-review` only after the cadence is Manual. Wiring it while the
// cadence is still Automatic would add a second review on the failure path.
// See docs/KODY-ONCE-PER-PR.md.

import { isTriggerComment } from '../kody-first-review/lib.mjs';

export const KODY_CHECK_NAME = 'Kody Code Review';
export const KODY_APP_SLUG = 'kody-ai';
export const KODY_APP_ID = 413034;
export const RECEIPT_TITLE = 'Code Review Complete';
export const RECEIPT_SUMMARY_PREFIX = 'Review finished successfully';
export const REREVIEW_LABEL = 'kody-rereview';

// GitHub check-run conclusions that mean the attempt did not produce a review.
// `skipped` and `neutral` are handled separately: they are not a receipt, and
// they are not a failed attempt that must be retried when an older receipt exists.
const FAILED_CONCLUSIONS = new Set([
  'failure',
  'timed_out',
  'cancelled',
  'stale',
  'action_required',
  'startup_failure',
]);

const READY_ACTIONS = new Set(['opened', 'ready_for_review', 'reopened']);

function appSlug(check) {
  const app = check?.app;
  if (typeof app === 'string') return app;
  return app?.slug || '';
}

export function isKodyCheck(check) {
  if (!check || check.name !== KODY_CHECK_NAME) return false;
  const slug = appSlug(check);
  const appId = check.app_id ?? check.app?.id;
  if (slug === KODY_APP_SLUG) return true;
  if (appId != null && Number(appId) === KODY_APP_ID) return true;
  return false;
}

function titleOf(check) {
  return String(check?.title || check?.output?.title || '');
}

function summaryOf(check) {
  return String(check?.summary || check?.output?.summary || '');
}

// The full-review receipt from docs/AUTO-MERGE.md. A success check with any
// other title or summary is not a completed review.
export function isFullReviewReceipt(check) {
  if (!isKodyCheck(check)) return false;
  if (check.status !== 'completed' || check.conclusion !== 'success') return false;
  if (titleOf(check) !== RECEIPT_TITLE) return false;
  return summaryOf(check).startsWith(RECEIPT_SUMMARY_PREFIX);
}

export function isFailedAttempt(check) {
  if (!isKodyCheck(check) || check.status !== 'completed') return false;
  if (FAILED_CONCLUSIONS.has(check.conclusion)) return true;
  if (check.conclusion === 'success' && !isFullReviewReceipt(check)) return true;
  return false;
}

function isInFlight(check) {
  if (!isKodyCheck(check)) return false;
  return check.status === 'queued' || check.status === 'in_progress' || check.status === 'pending';
}

function completedAt(check) {
  const parsed = Date.parse(check?.completed_at || '');
  return Number.isFinite(parsed) ? parsed : 0;
}

export function latestReceiptAt(checks) {
  let latest = null;
  for (const check of checks || []) {
    if (!isFullReviewReceipt(check)) continue;
    const at = completedAt(check);
    if (latest == null || at >= latest) latest = at;
  }
  return latest;
}

function latestCompleted(checks) {
  let best = null;
  let bestAt = -1;
  for (const check of checks || []) {
    if (!isKodyCheck(check) || check.status !== 'completed') continue;
    const at = completedAt(check);
    if (at >= bestAt) {
      best = check;
      bestAt = at;
    }
  }
  return best;
}

// A request counts only when its line is newer than the latest full receipt.
// A comment that predates that receipt already had its review.
export function hasFreshTrigger(comments, sinceMs) {
  for (const comment of comments || []) {
    if (!isTriggerComment(comment?.body)) continue;
    if (sinceMs == null) return true;
    const at = Date.parse(comment?.created_at || '');
    if (Number.isFinite(at) && at > sinceMs) return true;
  }
  return false;
}

function skip(reason) {
  return { action: 'skip', reason };
}

function review(reason) {
  return { action: 'review', reason };
}

// event.action is a pull_request action. event.draft is the PR's draft flag.
// event.label is set on a `labeled` event. checks are Kody check runs for the
// PR (any commit). comments are issue comments.
export function decideOnce(event, context = {}) {
  const action = event?.action;
  const checks = context.checks || [];
  const receiptAt = latestReceiptAt(checks);
  const explicitComment = hasFreshTrigger(context.comments, receiptAt);
  const labelRequest = action === 'labeled' && event?.label === REREVIEW_LABEL;

  if (labelRequest || explicitComment) {
    if (checks.some(isInFlight)) return skip('in-flight');
    return review(labelRequest ? 'label' : 'comment');
  }

  if (event?.draft && action !== 'ready_for_review') return skip('draft');

  if (checks.some(isInFlight)) return skip('in-flight');

  if (READY_ACTIONS.has(action)) {
    if (receiptAt != null) return skip('already-reviewed');
    return review('first-ready');
  }

  if (action === 'synchronize') {
    const last = latestCompleted(checks);
    if (last && isFailedAttempt(last)) return review('failed-attempt');
    if (receiptAt != null) return skip('already-reviewed');
    if (!last) return review('no-review-yet');
    return review('not-a-review');
  }

  return skip('unsupported-event');
}
