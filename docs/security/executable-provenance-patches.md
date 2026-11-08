# Executable same-path provenance patches

Source proposal only. Normal review, CI and a separately qualified installation are required before use. No file mode, scanner exemption, fixture registry or production configuration is changed by this proposal alone.

The existing same-path patch guards still fetch and verify the exact original file at the expected commit, scan the complete original and result for dedicated/provider and loaded credentials, reject new generic assignments, and require unchanged contiguous source lines for generic residual matches.

Existing100644 files retain the original createCommitOnBranch path. Existing100755 files use explicit git-data blob, tree and commit objects, preserving100755. Symlinks, missing files and submodules stay refused. The new path verifies content bytes, blob hash, executable mode, tree and exact parent before changing a branch. Unattached objects may remain when those checks fail; they do not prove the branch changed. After the first object POST, failed receipts, malformed responses and upstream rejections keep the durable outcome UNKNOWN and must never be replayed. Only pre-write guards are definite refusals.

One fixed GraphQL updateRefs mutation changes only the requested nondefault branch, with beforeOid equal to expected_head_sha, afterOid equal to the verified new commit and force false. No REST ref-update fallback or automatic retry is used. A failed/ambiguous mutation or mismatched final readback is not safe to replay. The final branch head is read back before success.

GitHub documents atomic updates and beforeOid semantics at https://docs.github.com/en/graphql/reference/git . A nonforce REST ref update alone is not an atomic expected-head check and is not used here.
