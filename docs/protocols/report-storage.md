# Repository Report Storage

Owner decision, October 30, 2026: keep reports with their repositories and collect them under one `Docs` folder beside the Code integration folders, grouped by the exact GitHub repository names.

## Authoritative Files

New public-safe reports, audits, reviews and overviews belong in `<Repo>/docs/reports/`.  Use a descriptive dated filename such as `2026-10-30-backup-audit.md`, and link it from `docs/reports/README.md`.  Save a substantial report as a file before presenting its final link; an untracked task-lane file is a draft, not a delivered report.

Existing historical records keep their original paths, including `docs/audits/`, `docs/reviews/`, `docs/research/` and `docs/rollouts/`.  The report index links those collections and individual records.  Do not rename archaeology records, archived effort logs or generated protocol mirrors to organize this library.  Operational instructions and runbooks remain at their existing canonical paths.

Private operational reports belong in the private Fleet-OPS repository under `docs/reports/`, with supporting evidence in `docs/reports/evidence/`.  Public repository indexes may describe the private collection, but must not reproduce private inventory, credentials, evidence or private document contents.  Never save credentials in a report or evidence file, including in a private repository.

PR comments and review threads retain their GitHub identities.  A saved review report should summarize its conclusions and link the relevant PR, findings, source revision and validation evidence.

## Central Collection

The central layout is:

```text
Code/
  Docs/
    README.md
    <Exact-GitHub-Repo-Name>/
      README.md
      documents/
        <original repository-relative paths>
      catalog.json
      manifest.json
      reports/
        <source-commit>-<collection-policy>-<catalog-policy>/
          catalog.json
          manifest.json
          README.md
```

Source-folder roles, repository profiles and inspected migration surfaces are defined in [Repository Layout And Migration Contract](repository-layout.md).

For example, the folders are `Usage-Monitor`, `Socratic-Trade`, `Congress.Trade` and `congress-trading-shared`, matching their GitHub repositories and Code integration folders.  The central collection contains copies with source provenance; the committed repository file is authoritative.  Each manifest records the source Git ref, exact commit and content hashes.  `catalog.json` is generated from the published Git tree and is the canonical per-repository file inventory; rows include original paths, Git modes, object IDs and types, byte sizes when available, inferred roles, selected-copy status and an exclusion reason where applicable.  Catalog policy `tracked-tree-roles-v2` records a null byte size and an explicit reason when Git metadata can identify a tracked blob but that blob is unavailable locally.  The collector disables Git lazy fetching, so computing an excluded file's size never fetches its contents.  Its structural inventory summarizes tracked top-level entries, application and deployment roots, build manifests, and package boundaries from those same Git paths.  The catalog reads Git metadata only and never reads excluded source file contents.  The repository README is a generated view of that catalog, grouped by canonical source path.  Rebuild the collection from those sources, and publish revisions through the normal branch, review and merge workflow.

The collection is owner-only because it can include private repositories.  It is a document directory, not another Git checkout or application runtime.  Never copy untracked files, dirty worktree content, environment files, credential stores or symlink targets into it.  A private draft must first be saved to its approved private repository before joining the collected library.

## Collection And Refresh

`scripts/collect-repo-documents.py` is an on-demand collector.  It reads published Git refs and writes the central copies and indexes without changing the source checkouts.  Collection policy `human-docs-v3` includes tracked Markdown, reStructuredText, AsciiDoc, plain text, PDF and TeX documents throughout the repository, including root instructions, handbooks, guides, runbooks, skills, plans and client documentation.  Office documents and Jupyter notebooks are included only under canonical documentation roots because their contents are binary or may contain embedded data.  HTML is also limited to documentation roots, except for recognized root-level documentation names.  It copies linked local images and document-root evidence files.  Credential stores, credential-shaped files, scratch, temporary, backup and archive paths, vendor and dependency trees, generated output, caches, test fixtures and snapshots, submodule contents, and symlink targets are excluded with catalog reasons; private Fleet-OPS audit paths still require the explicit opt-in below.  Use its preview and check modes before applying an update; conflicting user-edited output must be reviewed instead of overwritten.  Use explicit owned worktree sources when running through a server lane guard.

Collection version paths include the source commit and policy identifiers.  When a policy changes, the collector writes a new immutable manifest, catalog and report README under the new version path; it keeps prior versioned records and continues to read legacy manifests.  The root manifest, catalog and path-grouped README identify the current collection.  This avoids rewriting a historical manifest when the same source commit is collected under broader coverage.

The collector also stages proposed `docs/reports/README.md` files for publication from owned lanes.  It does not commit, push or merge those proposals, change an integration tree, install a schedule, or register a background process.

Refreshes preflight every repository before writing and hold an exclusive library lock through recovery and commit.  A private write-ahead journal preserves before-images so an interrupted update can be recovered on the next apply; recovery refuses independently edited output.  The journal has a 64 MiB safety limit; exceeding it stops the update and preserves rollback rather than discarding before-images.  Check mode never writes or recovers a pending transaction.  Public index proposals omit credential-adjacent operational reports while the owner-only collection retains their tracked source documents.

Private audit documents are excluded by default.  The explicit `--private-docs-repo` option accepts one exact repository from the verified private-repository allowlist, currently Fleet-OPS and Kodus-Config.  It only enables that repository's `docs/private-audits/` path; regular human documents from private repositories follow the same policy into the owner-only central collection.  The opt-in is recorded in that repository's manifest and must be supplied again on later refreshes.  It never adds private audit paths to public index proposals or permits environment, key or credential files.

On servers where the lane guard protects the whole Code root, creating or refreshing `Code/Docs` requires an owner-approved narrow placement exception.  Do not disable the guard or use another tool route to bypass its refusal.  Keep a complete preview in the owned lane until placement is authorized.

## Remote Sources Without A Safe Checkout

`scripts/export-repo-git-source.py` uses the installed `simple-git` CLI and an existing process `SIMPLE_TOOLS_BEARER`; it never reads a credential file, authenticates a new connection or changes a source repository.  Pass an exact `--owner-repo`, `--expected-repository-id`, `--visibility`, pinned default-branch `--commit` and new `--output` descriptor path.  Private sources also require the exact `--private-docs-repo` identity at export and import, from the existing verified private-repository allowlist.

The exporter verifies repository metadata and the default branch's signed commit proof, reconstructs its Git object ID and root tree, and walks bounded nonrecursive trees to verify every tracked path's object binding.  The branch response supplies the commit proof without fetching changed-file patches.  It requests only the documents and linked assets selected by the shared collection policy, pinned to that commit, and verifies each blob's object ID and length.  Excluded content is never requested to determine byte size; unavailable provider tree sizes remain null with an explicit catalog reason.  Unsupported signatures, missing or truncated trees, unrepresentable paths, content over the relay's 512 KiB limit, incomplete content and exhausted bounds fail the export.

The descriptor and adjacent `<descriptor>.receipt.json` are owner-owned mode 0600 files in a mode 0700 directory.  The separate provider receipt binds the repository identity, visibility, default ref, pinned commit, root tree and canonical descriptor SHA-256.  Import is offline and repeats the commit, tree, path, blob, size and selection checks before acquiring the collection writer lock.  The receipt detects export/import rebinding or accidental tampering; it is not an independently signed provider attestation, and local root compromise is outside this boundary.

Pass repeatable `--git-source-descriptor PATH` options to the named collector, first with `--check`, then through the authorized central collection route after review and merge.  `--repo-path` continues to mean a local repository directory.  Remote descriptors support the owner-only central collection and cannot generate public index previews.  Their catalogs and manifests record the source transport, identity, root tree, descriptor and receipt digests, while immutable version paths add a `git-relay` suffix.  Existing selection, additive writes, library locking, journal recovery and restrictive output modes remain shared with local sources.

## Backup And Verification

Versioned source reports are protected by their Git remotes and the existing source archive job where that job includes the repository.  The generated central collection can be rebuilt from the recorded source commits; its existence alone does not prove that an external-folder backup ran.  Do not claim a draft is backed up until its committed remote copy or another verified backup exists.

At delivery, verify source links, manifest hashes, exact repository names, privacy boundaries and preservation of pre-existing documents.  Include the report path in the task closeout so the owner can find the saved result.
