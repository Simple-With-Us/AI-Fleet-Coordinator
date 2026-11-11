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
  "seat_not_in_policy",
  "bearer_rejected",
  "verifier_unavailable",
  "no_app_credential",
  "repository_moved",
  "read_too_large",
]);

/** The relay's fixed strings for a renamed or transferred repository (GitHub
 * redirected the slug, OPS-1036) and for a well-formed read answer over the
 * relay's size bound.  Both are definite:  nothing ran upstream, and a retry
 * of the same call gets the same answer. */
const REPOSITORY_MOVED_ERROR = "repository moved";
const READ_TOO_LARGE_ERROR = "workflow read result too large";

/** A GitHub owner/name, strictly:  the only shape a relay `moved_to` may take. */
export const MOVED_SLUG_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/(?!.*\.\.)[A-Za-z0-9_.-]{1,100}$/;

/** The validated move details of a relay body:  `moved_to` only as a strict
 * owner/name, `repository_id` only as a positive safe integer.  Anything else
 * is dropped, never reflected. */
export function movedRepository(body) {
  const out = {};
  if (!body || typeof body !== "object" || Array.isArray(body)) return out;
  if (typeof body.moved_to === "string" && MOVED_SLUG_RE.test(body.moved_to)) out.moved_to = body.moved_to;
  if (Number.isSafeInteger(body.repository_id) && body.repository_id > 0) out.repository_id = body.repository_id;
  return out;
}

/** Caller-facing text for a moved repository:  ours, built only from the validated details. */
export function repositoryMovedText({ moved_to: movedTo, repository_id: repositoryId } = {}) {
  if (movedTo) return `This repository moved to ${movedTo} (it was renamed or transferred, and GitHub redirects the old name).  Call again with repo "${movedTo}".  Nothing ran upstream.`;
  const id = repositoryId ? ` (GitHub repository id ${repositoryId})` : "";
  return `This repository moved${id}:  it was renamed or transferred, and GitHub redirects the old name.  Its current owner/name could not be read or is outside this seat's relay policy.  Call again with the current owner/name.  Nothing ran upstream.`;
}

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
  if (error === "seat_not_in_policy") return { reason: "seat_not_in_policy" };
  if (error === "bearer_rejected") return { reason: "bearer_rejected" };
  if (error === "verifier_unavailable") return { reason: "verifier_unavailable" };
  if (error === "no_app_credential") return { reason: "no_app_credential" };
  if (error === REPOSITORY_MOVED_ERROR) return { reason: "repository_moved" };
  if (error === READ_TOO_LARGE_ERROR) return { reason: "read_too_large" };
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
  seat_not_in_policy: "git-relay has no shared seat for the identity this credential names.  The loaded seat policy has to include that seat.  Nothing was written.",
  bearer_rejected: "git-relay knows this seat, and the credential check did not confirm this bearer.  Nothing was written.",
  verifier_unavailable: "git-relay could not ask the credential checker, so this bearer was not confirmed.  Nothing was written.  Try again shortly.",
  no_app_credential: "git-relay authenticated this seat, but its GitHub App id or private key is missing or unusable.  Nothing was written.",
  repository_moved: repositoryMovedText(),
  read_too_large: "GitHub's answer to this read is larger than git-relay's read bound.  Ask for less per call:  a smaller per_page, or the paged verb (for example gr_wf_pr_reviews_page, gr_wf_pr_comments_page or gr_wf_compare_commits_page).",
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

// Same allowlist git_relay/github_client.py safe_error_diagnostics keeps.
// A present field that fails the shape drops the whole object.  message is
// never a field here, so GitHub's refusal prose cannot ride along.
const DIAGNOSTIC_CATEGORIES = new Set([
  "resource_not_accessible",
  "rate_limit",
  "bad_credentials",
  "not_found",
  "unknown",
]);
const DIAGNOSTIC_PERMISSIONS = new Set([
  "actions",
  "administration",
  "checks",
  "contents",
  "issues",
  "metadata",
  "pull_requests",
  "statuses",
  "workflows",
]);
const REQUEST_ID_RE = /^[0-9A-Fa-f]{4,8}(?::[0-9A-Fa-f]{4,8}){3}$/;

function permissionMap(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const out = {};
  for (const [name, level] of Object.entries(value)) {
    if (!DIAGNOSTIC_PERMISSIONS.has(name) || (level !== "read" && level !== "write")) return null;
    out[name] = level;
  }
  return Object.keys(out).length ? out : null;
}

/** Allowlisted github_diagnostics, or null.  Unknown keys are ignored. */
export function allowlistedGithubDiagnostics(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!DIAGNOSTIC_CATEGORIES.has(value.category)) return null;
  const out = { category: value.category };
  if (value.request_id !== undefined) {
    if (typeof value.request_id !== "string" || !REQUEST_ID_RE.test(value.request_id)) return null;
    out.request_id = value.request_id;
  }
  const hasPermissions = value.accepted_permissions !== undefined;
  const hasSets = value.accepted_permission_sets !== undefined;
  if (hasPermissions && hasSets) return null;
  if (hasPermissions) {
    const permissions = permissionMap(value.accepted_permissions);
    if (!permissions) return null;
    out.accepted_permissions = permissions;
  }
  if (hasSets) {
    if (!Array.isArray(value.accepted_permission_sets)) return null;
    if (value.accepted_permission_sets.length < 1 || value.accepted_permission_sets.length > 4) return null;
    const sets = value.accepted_permission_sets.map(permissionMap);
    if (sets.some((item) => item === null)) return null;
    out.accepted_permission_sets = sets;
  }
  return out;
}
