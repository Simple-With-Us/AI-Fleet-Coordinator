# Classic branch protection GitHub commands

Default-off own-App classic branch protection read, full-replace update and
delete on the existing workflow-verb custody path.  Tag protection is not
here on purpose:  its endpoints are closed down upstream ("removed after
August 30, 2024, use Repository Rulesets"), so tag rules are a rulesets
family, not these verbs.

`wf_branch_protection_get` projects a verified shape:  enforce_admins, the
required status checks (strict + contexts), pull request review requirements,
push restrictions (users, teams, apps as names) and the toggle flags, each
type-checked so a malformed or credential-shaped response fails the read.

`wf_branch_protection_update` is the upstream full replace:  the four core
groups (required_status_checks, enforce_admins, required_pull_request_reviews,
restrictions) must be present, and explicit null disables a group.  Every
field the request schema supports is exposed - status check bindings with
their app_id, dismissal restrictions and bypass allowances on the review
group, block_creations and the other toggles - so a full replace never
silently discards a safety setting the caller could not see, and nothing is
claimed to round-trip that was not verified.  Actor lists are bounded at the
documented 100 total.  After the PUT, a fresh GET in the same run must return
the mandatory upstream shape (an empty or anchorless response is not
evidence) and every sent value must match exactly, including check app_id
bindings and actor groups;  groups not sent must come back cleared.

Status check app bindings follow the upstream contract exactly:  sending a
check without app_id (or sending contexts only) triggers the documented
automatic binding - upstream selects the App that recently supplied that
context - and app_id -1 explicitly allows any App.  The verifier then
confirms the context set and every explicitly sent binding, and claims
nothing about an auto-selected binding;  a contexts-only update is
supported, not rejected as a false mismatch.
`wf_branch_protection_delete` requires the fresh GET to answer 404.  Every
failed or unexpected verification read collapses to UNKNOWN (502):  the
outcome key is held and an identical retry is refused with 409, so a retry
never repeats the write.  A cached replay returns the recorded outcome of the
first execution;  it is not a new current-state check.

Commit signature protection has its own guarded pair because the main PUT
does not carry it:  `wf_branch_protection_signatures_enable` verifies the 200
echo AND a fresh GET shows enabled, and
`wf_branch_protection_signatures_disable` requires the fresh GET to show
disabled (404 or an explicit disabled state).

Branch protection changes merge safety for everyone using the repository:
this code adds capability, not permission to execute;  invoking the writes
needs the seat's own authority, and the same App Administration grant as
REPO-ADMIN.md covers them.  Rulesets (branch and tag) are a separate family.
