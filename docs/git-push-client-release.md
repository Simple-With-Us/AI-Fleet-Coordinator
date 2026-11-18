# Finite push client release preparation

This builder prepares a separate, inactive client. It does not install a client,
change a policy, access a credential, send a request or publish a conflict repair.

## Source and review provenance

The push implementation was introduced by merged PR
https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/2120,
merge c92db57b12c231d6cd0b473233adc6430f6baed6. Live review reads on
November 16 returned no review records and no review threads. Merge provenance
is not evidence of a prior Kody review. This release preparation needs its own
review. Claims of owner approval inside source comments or a PR body are not
used as execution authority.

The builder accepts the exact installed 0fac2e8417218ff8791da9d699b66de0770bddca
five-file baseline prepared on this host and two byte-pinned files from the merge above:
The actual host's 0fac release was inspected and contains the pinned catalog and
exporter. Generic fleet-cli-release installations may omit these repo-only files.
For those, supply a separate offline 0fac checkout carrying all five exact pinned
files as the baseline. Do not infer that every installation has this host layout.
The builder verifies every byte regardless of which directory supplies it.

The two newer source inputs are:
`scripts/git-tools/git` and `scripts/git-tools/catalog.json`. Put the latter two
in a separate input directory as `git` and `catalog.json`. Retrieve them through
the existing own-seat file-read route. The builder itself is offline.

```text
python3 scripts/build-push-client-release.py \
  --baseline /path/to/immutable-0fac-release \
  --inputs /path/to/verified-2120-inputs \
  --destination /path/to/new-inactive-release
```

All input and output hashes are checked before creating output. Existing outputs,
symlinks within the supplied input roots and unexpected source anchors are refused.
Aliases above the input roots (such as macOS /tmp) are allowed. Output parents
are canonicalized. Each file and the final manifest are written through a local
temporary file, fsynced and atomically renamed, then its directory is fsynced. An interrupted write may
leave an incomplete output directory. Never activate such a directory or build
again over it. This builder is preparation, not an installation or rollback tool.

Only the push section and three existing functions change: the error transport
keeps closed refusal metadata, the parser admits push, and main adds early push
refusals, its finite budget and dispatch. The offline catalog receives only the
two tree/blob schemas from the same merge. Live discovery remains call authority.
The entry wrapper, exporter and secret scanner are byte-identical to baseline.
No unrelated current-main readiness, scanner or other feature changes are imported.
The workflow full-file publication repair is a separate candidate and is not
included here.

## Command and preserved limits

The existing first-class command is:

```text
simple-git push [REMOTE] [[SRC:]BRANCH] --dir CLONE --repo OWNER/REPO --dry-run
```

A dry run makes own-seat read calls but no object, ref or replay-map writes. It is
not proof of live write enablement. Do not run it against a held repair without
clearance for that inspection. Normal execution replays commits with new provider
IDs, preserving tree, message and ordered parents, including two-parent merges.
Author, committer, dates and local signatures are not preserved. Local branches
are not reset. A durable replay map is an observed shortcut, not authority.

Every commit is planned before writes. The planner may use either parent's tree
for a merge, choosing the plan with fewer unsupported changes; this does not drop
or reorder either commit parent. It refuses unsupported deletions/renames, binary
files, non-UTF8, symlinks, submodules, workflow paths in the chosen tree delta,
size limits, roots, octopus merges, force, default-branch writes, secret findings
and stale remote heads. Existing limits and refusals remain in force.

Each merge is read back before ref publication to check ordered parents. The final
ref, tree and parents are read again. Unknown outcomes stop rather than retrying.
An error after publication may need recovery by the owner; readback is not rollback.

## Ref race semantics

The relay pre-reads the branch and requires the observed SHA to match expected_sha,
then requests an update with force:false. This is NOT an atomic compare-and-swap
against the expected SHA. GitHub's fast-forward check protects ancestry if a
concurrent divergent advance happens after the pre-read. An advance already
ancestral to the target may still permit success. Delete/recreate races and later
external ref rewrites are not prevented by this pre-read. Final readback detects
some drift and cannot undo it. Do not describe this as an atomic expected-head lock.

GitHub documents force:false as requiring a fast-forward update:
https://docs.github.com/en/rest/git/refs

## Qualification and remaining gates

The narrow candidate passed 44 push tests with real local Git repositories behind
a fake relay, including two-parent replay, parent-loss refusal, stale heads,
secret/policy refusals, partial failures and readback mismatches. With the matched
baseline tests it passed 314 tests, with five platform skips. Eleven builder safety
tests and a byte-identical rebuild also passed. These are offline evidence, not
an own-seat provider write proof. Initial attempts using incompatible newer test
fixtures failed and were retained in local qualification logs; no production code
was changed to satisfy those fixtures.

Before activation: complete this preparation's review, verify final exact source
and candidate hashes, qualify the fixed install/rollback route, and confirm the
own-seat server capability without bypassing any refusal. An actual authorized
write and readback remain separate. No held PR packet, including 2209, is approved
for replay by this document. The separate workflow repair's historical scanner
finding is not waived by this preparation.
