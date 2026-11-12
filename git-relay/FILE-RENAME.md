# Bounded exact-blob rename

`wf_file_rename` is default-off own-App contents:write code. It renames one exact
regular blob on an explicitly named non-default branch, preserving its executable
mode and reusing its SHA without downloading content. Source/destination paths
are closed, at most eight components and 1,024 characters. Workflow paths,
symlinks, submodules, same/overlapping paths and existing destinations are refused.
No overwrite, force, author/committer override, arbitrary URL or branch aliases.

Pre-reads require exact branch/head, commit/tree identity and complete immediate
trees, each at most 1,000 entries. New directories are supported. One tree records
both deletion and addition; one child commit is attached with force:false. The
expected head is a pre-read, not GitHub compare-and-swap: a concurrent branch or
repository-settings change is not atomically locked. Divergent updates fail the
fast-forward check. No claim of branch-head atomicity or cross-repo rename.

A malformed/transient pre-read is definitely unsent. From the first tree POST,
any failed or unverifiable write stage stays UNKNOWN and non-retryable, including
a later ref refusal after objects may exist. Existing file-backed outcome storage
owns the hold; changing only the message cannot re-arm it, including after restart.
Offline synthetic tests exercise the real relay path. No file was renamed live.
Source capability is not permission to rename anyone's files or deploy this code.

Official schema verified for create tree, create commit and update reference:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
