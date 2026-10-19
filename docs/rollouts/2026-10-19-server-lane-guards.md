# Server Lane Guards

Refs AFC-405.

The owner requested the same lane placement on the server as on the Mac.  Paths are relative to the account running an agent: `~/Code/<Repo>` holds integration repositories and `~/apps/lanes/<Repo>/<seat>-<slug>` holds task worktrees.  On the audited server this account is root, so those paths start with `/root`; `/Code` itself does not exist.

## Shared Guard Contract

The shared shell guard refuses recognizable fleet `git worktree add` and `git worktree move` commands targeting the integration root or its descendants.  It also refuses fleet `git clone` and `gh repo clone` into that tree unless the destination is the exact registered integration folder for that source repository.  This exception allows onboarding an integration repository without allowing a task clone beside it.

The existing temporary-checkout rules still apply.  Reads, Git fetches, worktree listing and maintenance in existing integration trees remain available.  A denial points to the lane command and the correct repository folder.  Unknown shell expressions, tool errors and hook timeouts retain the existing fail-open behavior; this is a placement guard, not filesystem isolation.

## Server Inventory

The read-only audit found Claude Code, Codex and Muse Code installed.  A native server Codex guard already denied integration-tree worktrees and allowed the lanes root, with its own persisted trust.  Keep that registration intact.  Claude had no registered hook, and Muse had no installed plugins.

Existing worktrees changed location during the audit.  The final observation had only integration repositories directly under `~/Code`, and task worktrees under `~/apps/lanes`.  This task did not move or delete those trees and does not attribute another session's migration to its own work.

## Install And Verify

Use the existing installer from a fresh owned lane containing the reviewed revision:

```sh
cd ~/apps/lanes/AI-Fleet-Coordinator/<owned-lane>/scripts
python3 -m fleet_lanes.install_tools plan tools claude muse
python3 -m fleet_lanes.install_tools apply tools claude
python3 -m fleet_lanes.install_tools verify tools claude muse
```

Muse additionally needs the installer-generated plugin installed at user scope and its exact hook capability approved through `muse plugins`.  Inspect the generated manifest before approving it.  The owner authorized server platform hooks in this task.  The installer prints the exact commands; it does not perform Muse approval itself.

Test both an integration-root destination and the sanctioned lanes destination through the persisted hook command.  For Muse, also use `muse plugins hook test` with those fixtures so the native plugin dispatcher executes the installed hook.  Keep synthetic commands in fixture JSON; never execute the fixture's Git command.  The standalone installer probe proves the command works, while a native dispatcher probe additionally proves that the platform loads and runs it.

Start a new agent session to load newly installed hooks.  Existing sessions are not interrupted.  Platforms absent from this server require their supported installer target and verification when installed; this rollout does not claim coverage for absent binaries or tools without a hook surface.  Future platform support remains tracked in AFC-353 and AFC-387.
