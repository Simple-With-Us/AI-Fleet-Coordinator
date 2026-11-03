# Repository administration GitHub commands

Default-off own-App repository update and delete on the existing workflow-verb
custody path, with the new `administration` mint permission ceiling key and the
existing repo/action allowlists.  No raw endpoints or account changes.

`wf_repo_update` sends only the fields the caller names - rename, description,
homepage, default branch, privacy, feature toggles, merge-method toggles and
archive - and verifies every sent field in the response before returning
(cleared description/homepage come back as null and are accepted as cleared).
Strings are bounded at 1024 UTF-8 bytes and secret-scanned before any network
or durable ledger; a rename must match GitHub's repository-name shape.  At
least one field is required; the ST-MCP contract layer closes the argument
set and the relay enforces the non-empty change.  `wf_repo_delete` deletes the
repository itself and verifies the empty 204 result.

Both verbs are real, irreversible-feeling effects: archiving, renaming and
deleting change what people and automation see.  This code adds capability,
not permission to execute; invoking them needs the seat's own authority, and
UNKNOWN holds and no-replay behavior apply unchanged.

Activation dependency beyond code: each seat's GitHub App must be granted the
Administration repository permission on github.com and each installation must
accept it before a mint carrying `administration` can succeed; until then the
verbs fail closed at mint time.  Repository creation (organization-scoped
only, not the personal namespace), transfer, topics, branch protection,
collaborators and webhooks remain separate families.

Outcome proof: on first execution update verifies the PATCH echo AND a fresh
GET of the (possibly renamed) repository before reporting, and delete requires
a fresh GET to answer 404.  A cached replay returns the recorded outcome of
that first execution;  it is not a new current-state check, so a success
replayed after a recreate or rename must not be read as the live state.  The
`administration` ceiling key is typed-verb only: the raw HTTP `mint_token`
action refuses it (`RAW_MINT_EXCLUDED`), so the widened ceiling cannot be
exported as a caller-held token.
