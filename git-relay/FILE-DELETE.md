# Exact file deletion GitHub command

Default-off wf_file_delete removes an exact prior blob SHA from an explicitly named
non-default branch.  It never deletes workflow files or accepts arbitrary author,
committer, URL or force fields.  Own App contents:write, repo/action allowlists,
secret-scanned bounded commit message and existing durable UNKNOWN/no-replay apply.
The exact branch is resolved before deletion; qualified refs and aliases are refused.
The outcome identity uses repo/path/branch/prior_sha, excluding the commit message,
so rewording cannot re-arm an UNKNOWN deletion. GitHub enforces prior blob SHA, not a branch-head lock.  The default-branch check is
a pre-read, not atomic with repository settings.  Malformed successful write receipts
stay UNKNOWN; malformed pre-read is definitely unsent.  No file was deleted to test
this source.  Source capability is not authority to delete user files.

Standalone rename and workflow-file writes remain unimplemented.
Schema: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
