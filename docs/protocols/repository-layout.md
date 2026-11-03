# Repository Layout And Migration Contract

Owner direction, November 3, 2026: organize the actual application, library, service, tooling and support directories across repositories, with one authoritative file catalog and an additional central documentation collection.  Indexes alone do not complete this work.  Track delivery in AFC-601.

## Common Roles

The shared convention names what a directory contains.  It does not require every repository to contain every directory.

| Root | Contents |
| --- | --- |
| `apps/<target>/` | Independently built applications or deployed services, including native clients, web apps, servers, command-line products and workers. |
| `packages/<name>/` | Reusable first-party libraries with explicit public entry points and dependencies. |
| `scripts/` | Thin repository commands, maintenance entry points and wrappers.  A substantial independently built tool belongs under `tools/` or `apps/`, according to whether it is development tooling or a shipped service. |
| `tools/<name>/` | Reusable development, code-generation and verification tool packages. |
| `infra/` | Deployment descriptors and infrastructure configuration.  Credentials, mutable production data and credential stores do not belong in Git. |
| `docs/` | Tutorials, operating guides, reference, architecture, decisions, plans and dated reports, grouped by their purpose. |
| `tests/` | Cross-package or end-to-end tests and synthetic fixtures.  Package-local unit tests may remain beside their package. |
| `assets/` | Shared source assets.  App-specific resources stay with their application or package. |
| `vendor/` | Intentionally versioned third-party material with provenance and an update policy.  Dependency caches and build output are not source packages. |
| `archive/` | Explicitly retired source retained for a stated reason, outside active builds and deploys.  Moving material here does not authorize deleting its history. |

Use lowercase role directories and stable target names.  Inside a package, retain the build system's normal names and structure: SwiftPM `Package.swift`, `Sources/<Target>` and `Tests/<Target>`, Xcode projects and resources, Gradle modules and source sets, or a language's conventional source/test layout.

For a multi-platform product, use `apps/ios`, `apps/macos` and `apps/android` for platform build boundaries.  A workspace may contain multiple related targets without inventing a separate package for every screen.  Web and server code sharing one framework runtime remain one application until an actual independent build/deploy boundary warrants a split.  Browser extensions and independently deployed workers get explicit targets rather than becoming unrelated root folders.

These role names are a fleet convention.  They are not a claim that npm, SwiftPM or Gradle mandates an `apps` root.  The underlying package and dependency boundaries must remain valid under the relevant [npm workspace model](https://docs.npmjs.com/cli/v11/using-npm/workspaces/), [SwiftPM layout](https://www.swift.org/getting-started/library-swiftpm/) and [Gradle multi-project model](https://docs.gradle.org/current/userguide/multi_project_builds.html).

## Repository Profiles

| Profile | Rule |
| --- | --- |
| Multi-application product | Use role roots for independently built apps, shared packages, tooling and infrastructure.  Declare workspace/package relationships in the actual build manifests. |
| Single application or library | Its standard source/package layout may stay at the repository root.  Adding an empty wrapper directory offers no boundary by itself.  Additional independently built platforms or services use the common role roots. |
| Static site | Preserve public URL routes and hosting output paths.  A route directory is not an independently built application merely because it is a folder. |
| Distribution repository | Preserve ecosystem-required directories, such as a Homebrew tap's `Casks`. |
| Operations or policy repository | Organize inventories, handbook source, generated mirrors, deployment configuration and tools by their actual roles.  Existing source-of-truth and publication-approval boundaries remain in force. |
| Archived repository | Inventory and collect permitted documentation read-only.  Do not unarchive it, change its release state or revive its jobs to normalize its layout. |

An exception records the ecosystem or external contract it preserves.  A legacy folder name, an unknown owner or a broken import is a migration finding, rather than a permanent exception inferred from its existence.

## Authoritative Catalog

Generate the repository catalog from one published Git tree.  Every tracked entry is accounted for by path, Git mode, object identity, file size where available, role and inclusion/exclusion reason.  Source revision and collection-policy version identify the snapshot.  Human navigation is derived from that catalog; it is not a second manually maintained file inventory.

Keep the root README as the narrative entry point and link it to the catalog view, setup, verification and contribution instructions.  Preserve existing useful README content.  Report, decision and runbook indexes may remain focused navigation views, with the repository catalog as the complete file listing.  Git itself remains the source authority for tracked files; ignored runtime data and credentials are never silently treated as repository source.

The additional central documentation tree is `Code/Docs/<Exact-GitHub-Repo-Name>/`.  Its copies retain source-relative paths and immutable revision/hash provenance.  It includes human documentation outside a `docs` folder, while recording explicit credential-store, generated/vendor and fixture exclusions.  A clean collector transaction proves managed-output consistency; a separate coverage comparison establishes what was or was not collected.  See [report storage](report-storage.md).

Git symlinks preserve their target path, as [GitHub's tree model](https://docs.github.com/en/rest/git/trees#create-a-tree) documents.  A symlink to another checkout is not a portable copy of that checkout's contents.  During a source migration, use a temporary internal relative alias only when the affected build, archive, deployment and supported checkout environments have been tested.  Do not replace the authoritative central document copies with links to disposable worktrees.

## Inspected Migration Surfaces

This is the initial structural inventory, not a claim that the listed moves have shipped.  The generated catalog supplies the exact paths and source commits for each batch.

| Repository | Structural Work To Reconcile |
| --- | --- |
| AI-Fleet-Coordinator | Separate shipped services and native apps from substantial tool packages currently mixed into `scripts`, root service folders and workers; preserve launcher/import contracts during the package transition. |
| BotFleet | Reconcile `apps`, `server`, `src`, `electron`, `companion`, `ios`, `mac-control`, `native`, `shared` and worker infrastructure against the active native/server rewrite boundaries. |
| Clutch | Identify application boundaries across `src`, `engines`, `bridges`, `native` and `ios`; keep engine adapters separate from platform application shells. |
| CodeCaps | Preserve its root Swift package contract while giving the iOS companion and duplicated `script`/`scripts` responsibilities explicit homes. |
| Congress.Trade | Reconcile `app`, `clients/ios`, `services`, root manifests, analysis tooling and data; preserve the combined application runtime where appropriate. |
| ContactLogo | Reconcile `Apps/ContactLogoAndroid`, root Apple projects, `Sources`/`Tests`, `web`, prototypes under `backups` and vendored code. |
| DealDex | Reconcile root web/server source with `native/ios` and `native/android`, and distinguish database migrations from generated runtime data. |
| Fleet-OPS | Separate per-app inventory directories, handbook source, generated output, tools and infrastructure without changing privacy or handbook publication rules. |
| FleetLink | Identify worker, CLI and iOS build boundaries; coordinate with the existing auth/cutover owner before moving active files. |
| HogHunter | Reconcile root Apple project sources with `ios`, `macos`, launchd configuration and scripts while preserving package/project conventions. |
| Personal-Site | Identify separate `site` and `start` applications, static assets and hosting entry points; update deployment references with each source move. |
| Simple-With-Us | Keep product-page URL directories as routes, distinguish the static catalog from deployable applications, and centralize shared build/assets responsibilities. |
| Socratic-Trade | Reconcile the framework's `app` and `src` roles with iOS, scripts, fixtures and design material; retain the framework's route semantics. |
| Usage-Monitor | Separate the web runtime, iOS workspace, retained Mac source, extensions and workers; coordinate with current authentication and telemetry edits. |
| congress-trading-shared | Keep the single published library root; separate release/development tooling and prevent shared contracts from leaking application ownership. |
| homebrew-tap | Keep Homebrew's required `Casks` structure; use the same catalog and maintenance conventions without inventing an application workspace. |

The original central library covered these sixteen local sources.  The accessible organization inventory additionally contains archived Autorotate and private Kodus-Config; include their catalog/document coverage with the appropriate profile and privacy policy.  A missing local checkout does not prove an organization repository is absent.

## Deliver Each Structural Batch

1. Record the current tree, exact source/destination paths, actual app/package boundary and active file reservations.  Read all relevant build manifests and ownership instructions before choosing a move.
2. Preserve existing work in owned lanes and coordinate source ownership.  Move one coherent application, package or support area at a time with Git history retained.
3. Update imports, manifests, project files, test fixtures, code generation, scripts, CI working directories/path filters, container contexts and deployment roots affected by that move.  Keep product identifiers, API contracts, public routes and data locations stable unless that batch explicitly changes them.
4. Run the relevant language/build checks and required hosted platform jobs.  For deployed applications, verify the published revision, startup, public/authenticated surfaces appropriate to the change, and data/backup continuity separately from CI.
5. Read and address every review surface on the final head, merge through the repository's required path, regenerate the catalog and central documentation from published sources, and verify coverage and links.

A batch is complete when the source has the agreed role boundary and its consumers work at the published revision.  A new README, catalog, symlink or unmerged rename proposal alone does not complete a structural migration.
