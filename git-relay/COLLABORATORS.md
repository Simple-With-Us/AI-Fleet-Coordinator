# Repository collaborator GitHub commands

Default-off own-App collaborator list, permission read, add/invite and remove on
the existing workflow-verb custody path, with repository metadata reads and the
`administration` mint permission for writes.  No raw endpoints or account
changes.

`wf_collaborators` returns a bounded page of login and role only; malformed or
credential-shaped rows fail the read.  `wf_collaborator_permission` verifies
the returned user matches the requested login and the level is a known
permission.  `wf_collaborator_add` takes an optional permission (pull, triage,
push, maintain, admin) and reports honestly whether the member was added
directly (empty completion) or an invitation was created (verified invitation
id and invitee login).  `wf_collaborator_remove` verifies the empty 204
result.  Usernames must match GitHub's login shape and are secret-scanned
before any network or durable ledger.

Adding and removing collaborators changes who can reach a repository: this
code adds capability, not permission to execute; invoking the writes needs
the seat's own authority, and UNKNOWN holds and no-replay behavior apply
unchanged.  The same App Administration grant as REPO-ADMIN.md covers the
writes; pending invitations and organization/team membership remain separate
families.

Direct versus effective access: a direct repository grant is different from
effective access, which team memberships, default organization permissions and
ownership can also produce.  The check-collaborator and permission routes
report EFFECTIVE access (the permission endpoint's `permission` is the legacy
base admin, write, read or none; `role_name` is the highest role from all
grant sources), so they never prove a direct grant exists or is gone.

Direct evidence: the only supported direct-affiliation route is the list with
`affiliation=direct`, which GitHub documents as collaborators with permissions
to the repository regardless of organization membership status;  the response
cannot distinguish organization-level from repository-level grants, so no
stronger exclusion of inherited access is claimed.  The evidence is used in
its fail-closed direction only:  presence is meaningful on any page (a found
login is there), while absence is evidence only from a complete bounded page -
a saturated page proves nothing and is unverifiable.

Outcome proof: on first execution every write verifies against fresh reads in
the same run.  Add (204 completion) requires the user present in the
direct-affiliation list;  remove requires them absent from a complete bounded
page.  Add also reads the effective permission separately and requires it to
be at least the requested base level.  The response keeps the facts separate:
`requested_permission` is what was asked (the 204 semantics created the
individual grant at that level), `effective_permission` and `effective_role`
are the verified observation, which team or organization grants can raise;
the direct role itself is not separately verifiable from upstream and is not
claimed.  Invitations re-read the bounded official invitation list and match
id, invitee and requested permission.  Every failed or unexpected
verification read collapses to UNKNOWN (502):  the outcome key is held and an
identical retry is refused with 409, so a retry never repeats the write.  A
cached replay returns the recorded outcome of the first execution;  it is not
a new current-state check.

`wf_collaborators` takes an optional `affiliation` filter (all, direct,
outside);  without it the upstream default lists everyone the caller can see,
including team-inherited and organization access.
