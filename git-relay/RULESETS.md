# Repository rulesets GitHub commands

Default-off own-App ruleset reads and writes on the existing workflow-verb
custody path.  This is also where tag protection lives:  the dedicated tag
protection endpoints were closed down upstream ("removed after August 30,
2024, use Repository Rulesets"), so branch and tag rules both come from
these verbs.

Reads.  `wf_rulesets` lists a bounded page projected to id, name, target and
enforcement per row;  any malformed row fails the read.  `wf_ruleset_get`
projects one ruleset with verified shape:  bypass actors (actor id, actor
type, bypass mode), ref-name conditions, and the rules.  `wf_branch_rules`
lists the rules applying to one branch (type, ruleset id, ruleset source).

Rule coverage.  Every rule type the upstream API documents is verifiable
end to end, with parameters type-checked exactly per type:  creation,
update, deletion, required_linear_history, required_signatures,
non_fast_forward, license_compliance_scanning, pull_request (including
allowed_merge_methods, dismissal_restriction allowed actors and
required_reviewers), required_status_checks (including
do_not_enforce_on_create), required_deployments, merge_queue, workflows,
code_scanning, code_quality, code_coverage, copilot_code_review,
file_path_restriction, max_file_path_length, file_extension_restriction,
max_file_size, and the five pattern rules (commit_message_pattern,
commit_author_email_pattern, committer_email_pattern, branch_name_pattern,
tag_name_pattern).  A response rule whose type or parameters deviate from
the documented shape fails the read closed.

Verification and documented defaults.  Post-write verification compares
only the groups the caller sent, in a comparison form that fills the
documented server defaults:  an omitted pull_request
allowed_merge_methods equals the returned full method list, an omitted
require_extra_approval_for_unattributed_changes equals false, and an
omitted do_not_enforce_on_create equals false.  Any non-default deviation
is UNKNOWN.  A validation fault while projecting a response AFTER a write
is always UNKNOWN (502) - never a local refusal that would release the
outcome key and allow a repeated mutation.

Writes.  `wf_ruleset_create` takes the ruleset name, enforcement (disabled,
active, evaluate), optional target (branch, tag, push), optional bypass
actors, conditions and rules.
`wf_ruleset_update` is the upstream partial update:  every group is
optional (including name, enforcement and rules, matching the documented
PUT contract), at least one must be sent, and an omitted group is left
unchanged upstream - it is never transmitted as an empty value that would
silently clear existing protections.  Sending an explicit empty rules array
clears the rules and must verify cleared.  Verification compares only what
was actually requested.
After the POST/PUT, a fresh GET in the same run must return the same
ruleset id and match every sent value exactly - name, enforcement, target,
bypass actors as a set, conditions, and the full rules list with each
rule's parameters.  `wf_ruleset_delete` requires the fresh GET to answer
404.  Every failed or unexpected verification read collapses to UNKNOWN
(502):  the outcome key is held and an identical retry is refused with 409,
so a retry never repeats the write.  A cached replay returns the recorded
outcome of the first execution;  it is not a new current-state check.

Rulesets change merge safety for everyone using the repository:  this code
adds capability, not permission to execute;  invoking the writes needs the
seat's own authority, and the same App Administration grant as the rest of
the administration family.
