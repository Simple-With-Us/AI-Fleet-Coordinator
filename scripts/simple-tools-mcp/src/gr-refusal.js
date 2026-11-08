// Why did git-relay refuse?  A closed, allowlisted `reason` for a relay 4xx.
//
// A relay refusal used to reach the caller as a bare relay_refused (or, on the
// workflow path, the flat workflow_request_refused), so Echo and Wise could
// not tell a seat-policy denial from a GitHub merge block or a head-fence
// mismatch (AFC-623, Nov 5-6 2026).  The relay already knows the cause and
// records it in its audit row:  `denied` for a seat-policy refusal,
// `bad_request` with a fixed reason string, `github_terminal` with GitHub's
// own status.  This module maps ONLY those exact, fixed relay strings (and the
// numeric github_status) to a small closed vocabulary.
//
// Rules (the same ones the credential_scan marker follows):
//  - the relay's prose is never reflected:  a string that is not matched
//    exactly (or by an anchored, bounded pattern) yields null, and the caller
//    keeps the flat answer it had before;
//  - the result is a member of REFUSAL_REASONS or null, nothing else;
//  - github_status is an integer 400..499, validated, never copied as text.
//
// The relay's error bodies carry no machine code of their own today and need
// none for this:  every class below is derivable from a fixed string or the
// existing numeric github_status, and the hosted front has to classify against
// the relay that is deployed now, not one that ships later.

/** relay string -> reason id, for the relay's fixed 400 verdicts. */
const BAD_REQUEST_REASONS = new Map([
  ["pull requests are not issue lifecycle targets", "bad_request_pr_not_issue_target"],
  ["default branch mutation refused", "bad_request_default_branch_protected"],
  ["default branch is not writable", "bad_request_default_branch_protected"],
  ["PR not open", "bad_request_pr_not_open"],
  ["a merged PR cannot change state", "bad_request_pr_merged"],
  ["workflow permission required for this path", "bad_request_workflow_path_permission"],
  ["only pending reviews may be deleted", "bad_request_review_not_pending"],
  ["issue update requires a change", "bad_request_nothing_to_change"],
  ["nothing to edit", "bad_request_nothing_to_change"],
  ["invalid files", "bad_request_invalid_files"],
  ["invalid workflow arguments", "bad_request_invalid_arguments"],
  ["unexpected argument", "bad_request_invalid_arguments"],
  ["unsupported workflow action", "bad_request_unsupported_action"],
  ["invalid repository", "bad_request_invalid_repository"],
  ["files too large", "bad_request_content_too_large"],
  ["file too large", "bad_request_content_too_large"],
  ["file content budget exceeded", "bad_request_content_too_large"],
]);

/** The relay's fixed head, ref and identity fences:  the caller's expected
 * state no longer matches what the relay read just before the write. */
const FENCE_MISMATCH_ERRORS = new Set([
  "PR head not matched",
  "PR head moved",
  "PR base not matched",
  "PR head repo not matched",
  "pull request head changed",
  "pull request number/head not matched",
  "queue PR head or state changed",
  "branch head moved",
  "exact branch head not matched",
  "head branch not at expected sha",
  "ref changed",
  "commit not matched",
  "check identity or commit changed",
  "comment body changed",
  "workflow identity, attempt or state not matched",
  "label identity does not match",
  "release asset does not match expected_name",
]);

/** Seat-policy denials (the relay's `denied` audit outcome).  The two
 * parameterised strings are policy.py's `action {action!r} ...` and
 * `repo {repo!r} ...`, anchored and bounded so a near-match stays unmapped. */
const SEAT_POLICY_ERRORS = new Set([
  "unknown seat",
  "seat permission ceiling insufficient",
  "own App required",
  "action or repository denied",
  "action or repository not authorized for this handle",
]);
const SEAT_POLICY_PATTERNS = [
  /^action '[A-Za-z0-9_]{1,64}' not allowed for this seat$/,
  /^repo '[A-Za-z0-9._\/-]{1,200}' not allowed for this seat$/,
  /^seat ceiling does not cover permission '[A-Za-z0-9_:-]{1,64}'$/,
];
const SEAT_RATE_LIMIT_ERRORS = new Set([
  "hourly rate limit reached for this seat",
  "hourly write limit reached for this seat",
]);

const REFUSAL_BASE_REASONS = Object.freeze([
  "credential_scan",
  "seat_policy",
  "seat_rate_limit",
  "github_refused",
  "fence_mismatch",
  "unauthenticated",
]);

/** Every reason this module can produce, in a stable order. */
export const REFUSAL_REASONS = Object.freeze([
  ...REFUSAL_BASE_REASONS,
  ...[...new Set(BAD_REQUEST_REASONS.values())].sort(),
]);

const REASON_SET = new Set(REFUSAL_REASONS);

/** Is `value` one of the closed reason ids?  Anything else - a string a relay
 * made up, a wrong type - is not a reason. */
export function isRefusalReason(value) {
  return typeof value === "string" && REASON_SET.has(value);
}

/** `value` when it is a terminal GitHub client-error status, else undefined. */
export function githubRefusalStatus(value) {
  return Number.isInteger(value) && value >= 400 && value <= 499 && value !== 429 ? value : undefined;
}

/**
 * Classify one relay refusal.  `error` is the relay's own string (already
 * length-checked by the caller's schema) and `githubStatus` its numeric
 * github_status.  Returns {reason, github_status?} or null.  credential_scan
 * is decided by the caller, which owns the vouched-family check.
 */
export function classifyRelayRefusal(error, githubStatus) {
  if (typeof error !== "string") return null;
  if (error === "github refused the request") {
    const status = githubRefusalStatus(githubStatus);
    return status === undefined ? null : { reason: "github_refused", github_status: status };
  }
  if (error === "unauthenticated") return { reason: "unauthenticated" };
  if (SEAT_RATE_LIMIT_ERRORS.has(error)) return { reason: "seat_rate_limit" };
  if (SEAT_POLICY_ERRORS.has(error) || SEAT_POLICY_PATTERNS.some((re) => re.test(error))) return { reason: "seat_policy" };
  if (FENCE_MISMATCH_ERRORS.has(error)) return { reason: "fence_mismatch" };
  const badRequest = BAD_REQUEST_REASONS.get(error);
  return badRequest ? { reason: badRequest } : null;
}

/** The fixed sentence a caller sees for a reason:  ours, never the relay's. */
const REASON_TEXT = Object.freeze({
  credential_scan: "git-relay refused the write:  the content looks like it contains a credential.  Remove or reword it and send it again.  Nothing was written.",
  seat_policy: "git-relay's seat policy does not allow this action or repository for this seat (or the seat is unknown or its App ceiling is too low).  Retrying will not help;  the seat's relay policy has to change.",
  seat_rate_limit: "This seat reached its git-relay hourly call or write quota.  Wait for the quota window, then retry.",
  github_refused: "GitHub itself refused the request (see github_status:  403 is permission or a branch rule, 405 is a merge block, 422 is a rejected change).  The relay passed it on;  fix the cause on GitHub, not in the seat policy.",
  fence_mismatch: "The pull request, branch, comment or check no longer matches the head, base or state the call expected.  Read the current state, then send the call again with the fresh value.",
  unauthenticated: "git-relay did not accept this seat's credential.  Nothing was written.",
  bad_request_pr_not_issue_target: "The target is a pull request, not an issue.  Use the pull-request tool for it.",
  bad_request_default_branch_protected: "The relay refuses to write to or move the default branch.  Use a feature branch and a pull request.",
  bad_request_pr_not_open: "The pull request is not open.",
  bad_request_pr_merged: "The pull request is already merged and cannot change state.",
  bad_request_workflow_path_permission: "The change touches a workflow path, which needs the workflow permission this seat's App does not have.",
  bad_request_review_not_pending: "Only a pending review can be deleted.",
  bad_request_nothing_to_change: "The call would change nothing.  Send at least one field that differs.",
  bad_request_invalid_files: "The files argument is not acceptable.  Check each path and content.",
  bad_request_invalid_arguments: "The relay rejected the arguments as invalid or unexpected.  Check the tool's schema.",
  bad_request_unsupported_action: "The relay does not support this workflow action.",
  bad_request_invalid_repository: "The repository is not a valid owner/name.",
  bad_request_content_too_large: "The content is over the relay's size or budget limit.  Send less per call.",
});

/** Fixed caller-facing text for a reason id, or null when there is none. */
export function refusalReasonText(reason) {
  return isRefusalReason(reason) ? (REASON_TEXT[reason] ?? null) : null;
}
