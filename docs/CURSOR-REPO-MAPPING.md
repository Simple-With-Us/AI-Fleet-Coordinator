# Cursor Folder & Repository Mapping Architecture

This document details how Cursor binds local filesystem folders to remote GitHub repositories, why cloud-agent task delegation from Grok Bot failed after the GitHub organization migration, where to find and verify these settings in Cursor, and how the mappings are repaired.

---

## 1. Root Cause Analysis: The Stale Organization Cache

When fleet repositories were migrated from `jaywedgeworth22/<repo>` to `Simple-With-Us/<repo>`, all local git checkouts in `/Users/jay/Code/*` had their remote origins correctly updated to `https://github.com/Simple-With-Us/<repo>.git`.

However, Cursor retains internal SQLite state databases that cache the initial repository URL bound to each folder:

1. **`workspaceMetadata.entries`** (`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`):
   Contains an array of all workspace directories opened in Cursor.  For each folder, it maintains `trackedGitRepos` with `repoPath` and `repoUrl` (e.g. `github.com/jaywedgeworth22/socratic.trade`).
2. **`repositoryTracker.paths`** (`state.vscdb`):
   Maps canonical GitHub repository keys (e.g. `github.com/jaywedgeworth22/congress.trade`) to local filesystem URIs (`file:///Users/jay/Code`).
3. **`cursor/glass.projectSelector.recentCloudTargets` & `recentPrivateWorkers`** (`state.vscdb`):
   Caches recent target repositories and worker repo labels (e.g. `repoLabel: "jaywedgeworth22/congress-trading-shared"`).
4. **`workbench.backgroundComposer.workspacePersistentData`** (`~/Library/Application Support/Cursor/User/workspaceStorage/<workspace-id>/state.vscdb`):
   Caches `cachedSelectedRemote.url` per workspace.
5. **Background Worker Daemon (`cursor-agent-worker`)**:
   Cursor spawns a persistent daemon (`cursor-agent worker start --worker-dir <path>`) for Background Composer / Cloud Agent tasks.  Upon startup, it logs:
   ```text
   INFO Derived repo label from git origin ctx=worker-mode meta={repo: "jaywedgeworth22/ai-fleet-coordinator"}
   ```
   If Cursor was left running across the GitHub org rename, or if the daemon reparented to `launchd`, the daemon stayed connected to `api2.cursor.sh` advertising the stale `jaywedgeworth22/<repo>` label.

### The Breakdown with Grok Bot
When Grok Bot (`[GB-*]`) dispatched a task to Cursor Cloud Agents targeting `Simple-With-Us/<repo>`, the Cursor backend attempted to route the job to a private worker advertising that repository.  Because the daemon held the stale `jaywedgeworth22/<repo>` label and `state.vscdb` cached the old repo URL, routing failed with "no worker found for repo" or could not associate the workspace folder.

---

## 2. Where to View & Change Settings in Cursor UI

Cursor separates standard VS Code settings from Cursor-specific agent settings:

### Primary Settings Surface
1. Open Cursor Settings:
   - Shortcut: **`Cmd + Shift + J`**
   - Or Menu Bar: **Cursor** → **Cursor Settings** (or the gear icon in the top right of the window → **Cursor Settings**).
2. Look at the left navigation sidebar:
   - **Features** → **Composer** / **Cloud Agents**:
     - Lists all connected GitHub accounts and repositories.
     - Under **Repositories**, check which repositories are synchronized with your Cloud Agent workspace.
   - **Features** → **Private Workers** (or **My Machines**):
     - Shows all active private workers registered on this Mac.
     - Displays the machine name, worker ID, connected status, and the repository / directory each worker is bound to.
3. If an individual repository needs re-authorization:
   - In **Cursor Settings** → **Features** → **Cloud Agents**, click **Manage GitHub Repositories** to ensure the `Simple-With-Us` organization is granted access in your GitHub OAuth App settings.

---

## 3. Automated Database Repair

All Cursor databases were inspected and updated in place with full APFS clone backups:

- **Backup created**: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb.bak-ag-20261004`
- **Tracked repositories updated**: 17 entries across `AI-Fleet-Coordinator`, `Socratic.Trade`, `Congress.Trade`, `Usage-Monitor`, `DealDex`, `Personal-Site`, `ContactLogo`, `HogHunter`, `Autorotate`, and `congress-trading-shared`.
- **Project selector & private workers updated**: All `jaywedgeworth22` references updated to `Simple-With-Us`.
- **Cloud repo branch keys migrated**: 12 `cloudRepoBranches` and `cloudRepoBranchRecency` keys cloned to `Simple-With-Us`.
- **Workspace storage databases updated**: `workbench.backgroundComposer.workspacePersistentData` updated across all individual workspace directories.
- **Worker process bounced**: Terminated stale daemon PID 85577.  The next time Cursor launches, it will spawn a fresh worker deriving `Simple-With-Us` from origin.
