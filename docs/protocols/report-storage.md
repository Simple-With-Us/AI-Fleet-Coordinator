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
      manifest.json
```

For example, the folders are `Usage-Monitor`, `Socratic-Trade`, `Congress.Trade` and `congress-trading-shared`, matching their GitHub repositories and Code integration folders.  The central collection contains copies with source provenance; the committed repository file is authoritative.  Each manifest records the source Git ref, exact commit and content hashes.  Rebuild the collection from those sources, and publish revisions through the normal branch, review and merge workflow.

The collection is owner-only because it can include private repositories.  It is a document directory, not another Git checkout or application runtime.  Never copy untracked files, dirty worktree content, environment files, credential stores or symlink targets into it.  A private draft must first be saved to its approved private repository before joining the collected library.

## Collection And Refresh

`scripts/collect-repo-documents.py` is an on-demand collector.  It reads published Git refs and writes the central copies and indexes without changing the source checkouts.  Use its preview and check modes before applying an update; conflicting user-edited output must be reviewed instead of overwritten.  Use explicit owned worktree sources when running through a server lane guard.

The collector also stages proposed `docs/reports/README.md` files for publication from owned lanes.  It does not commit, push or merge those proposals, change an integration tree, install a schedule, or register a background process.

Refreshes preflight every repository before writing and hold an exclusive library lock through recovery and commit.  A private write-ahead journal preserves before-images so an interrupted update can be recovered on the next apply; recovery refuses independently edited output.  The journal has a 64 MiB safety limit; exceeding it stops the update and preserves rollback rather than discarding before-images.  Check mode never writes or recovers a pending transaction.  Public index proposals omit credential-adjacent operational reports while the owner-only collection retains their tracked source documents.

Private audit documents are excluded by default.  The owner-only central collection may include the verified private Fleet-OPS audit directory only with the explicit `--private-docs-repo Simple-With-Us/Fleet-OPS` option.  That choice is recorded in the private manifest and must be supplied again on later refreshes.  It never adds private audit paths to the public index proposals or permits environment, key or credential files.

On servers where the lane guard protects the whole Code root, creating or refreshing `Code/Docs` requires an owner-approved narrow placement exception.  Do not disable the guard or use another tool route to bypass its refusal.  Keep a complete preview in the owned lane until placement is authorized.

## Backup And Verification

Versioned source reports are protected by their Git remotes and the existing source archive job where that job includes the repository.  The generated central collection can be rebuilt from the recorded source commits; its existence alone does not prove that an external-folder backup ran.  Do not claim a draft is backed up until its committed remote copy or another verified backup exists.

At delivery, verify source links, manifest hashes, exact repository names, privacy boundaries and preservation of pre-existing documents.  Include the report path in the task closeout so the owner can find the saved result.
