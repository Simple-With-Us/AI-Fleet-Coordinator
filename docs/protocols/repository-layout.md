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

## Multi-Platform Repository Example

Use build and deployment boundaries to choose application folders.  The example below is illustrative; create only the directories the repository needs and retain each platform's normal project structure inside its application.

```text
repo/
├── README.md
├── AGENTS.md
├── .github/workflows/
├── apps/
│   ├── ios/                 # Xcode project, targets and resources
│   ├── macos/               # Mac application
│   ├── android/             # Gradle project and modules
│   ├── web/                 # Browser application
│   ├── marketing/           # Independently deployed public website
│   ├── server/              # Independently deployed backend, when needed
│   └── browser-extension/   # Independently built extension, when needed
├── packages/
│   ├── product-core/        # Reusable business logic
│   ├── api-contracts/       # Schemas and protocol definitions
│   ├── apple-core/          # Shared Swift package
│   ├── web-ui/              # Shared web components
│   └── design-tokens/       # Shared colors, spacing and typography
├── assets/brand/            # Shared original logos and artwork
├── docs/
│   ├── architecture/
│   ├── decisions/
│   ├── guides/
│   ├── runbooks/
│   └── reports/
├── infra/                   # Deployment and infrastructure configuration
├── scripts/                 # Thin repository commands
├── tools/                   # Substantial development tools
└── tests/e2e/               # Tests spanning applications
```

Give reusable packages specific names and explicit public interfaces.  Keep application-specific feature code, tests and resources with their application; extract shared code when another consumer needs it.  Declare dependencies in the relevant manifests rather than importing another application's internals through relative paths.  This follows the application/package separation in [Turborepo's repository guidance](https://turborepo.dev/docs/crafting-your-repository/structuring-a-repository) and deliberate sharing in [Nx's folder guidance](https://nx.dev/docs/kb/folder-structure).

### Marketing Website

Use `apps/marketing/` when the public website has its own build and deployment.  Keep its content, SEO metadata, public assets and site-specific components there; shared brand originals belong in `assets/brand/`, and reusable web components or design tokens belong in named packages.

When marketing and authenticated pages share one Next.js runtime, keep them in `apps/web/`, organized with route groups such as `src/app/(marketing)/` and `src/app/(product)/`.  Those grouping names do not add URL segments.  Preserve framework route and asset conventions, as described in [Next.js project organization](https://nextjs.org/docs/app/getting-started/project-structure).  A separate marketing deployment needs its own configured deployment root, build command and output settings; sharing a repository does not require sharing a release cycle, as illustrated by [Vercel's monorepo deployment model](https://vercel.com/docs/monorepos).

## Affected Builds And Deployments

Build, test, package and deploy only the applications affected by a change.  A commit to the repository is not by itself a reason to rebuild or release every platform.  An application is affected when its own source or resources change, a direct or transitive dependency changes, or a relevant manifest, lockfile, code generator, build configuration or deployment configuration changes.

| Change | Required Scope |
| --- | --- |
| iOS-only source or resources | Run the iOS checks and build; leave the website, Mac and Android releases untouched unless they consume the changed inputs. |
| Marketing-only source, content or assets | Check and build the marketing website and deploy it through its normal release policy; leave native application releases untouched. |
| Web application-only source | Check and build the web application; include marketing pages when they share that runtime. |
| Shared library, API contract, design tokens or brand asset | Check the changed package or asset and every application that consumes it, including transitive consumers. |
| Build tooling, manifests, lockfiles or infrastructure | Determine which targets consume the changed inputs and run their affected checks/builds; expand to all targets only when the change actually has repository-wide scope or the affected set cannot be established safely. |
| Documentation-only changes | Run documentation, link and applicable repository-policy checks; do not build or release application binaries unless the documentation is itself a deployed application's input. |

Implement this selection in CI and deployment triggers using maintained path mappings plus the actual dependency graph.  Include shared inputs in each consumer's trigger, account for renamed and deleted paths, and compare against the appropriate pull-request base or last deployed revision.  A path filter limited to `apps/ios/**` misses changes to an iOS dependency under `packages/`; a root lockfile change needs dependency analysis rather than an automatic assumption that every native app changed.

Keep required pull-request checks reportable even when no application build is needed: use an always-running change classifier and required aggregate gate that reports success only after the selected checks pass.  Do not leave a required workflow permanently pending by filtering away its entire trigger.  Keep build selection separate from release authorization; a successful affected build does not itself authorize an App Store release or production deployment.

Each migration batch must verify representative iOS-only, website-only, shared-dependency and documentation-only changes against its selection rules.  Record the selected and skipped targets, then verify that the hosting provider's automatic deployment behavior agrees with CI; CI filters alone do not prevent a separate hosting integration from deploying unrelated commits.

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
4. Run the relevant language/build checks and required hosted platform jobs for the affected applications and their dependency graph, following Affected Builds And Deployments above.  Update and verify selection rules as paths move so unrelated platforms do not build or deploy.  For deployed applications, verify the published revision, startup, public/authenticated surfaces appropriate to the change, and data/backup continuity separately from CI.
5. Read and address every review surface on the final head, merge through the repository's required path, regenerate the catalog and central documentation from published sources, and verify coverage and links.

A batch is complete when the source has the agreed role boundary and its consumers work at the published revision.  A new README, catalog, symlink or unmerged rename proposal alone does not complete a structural migration.
