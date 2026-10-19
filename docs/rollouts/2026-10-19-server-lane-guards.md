# Server Lane Guards

Refs AFC-405.

The owner requested the same lane placement on the server as on the Mac.  Paths are relative to the account running an agent: `~/Code/<Repo>` holds integration repositories and `~/apps/lanes/<Repo>/<seat>-<slug>` holds task worktrees.  On the audited server this account is root, so those paths start with `/root`; `/Code` itself does not exist.

## Shared Guard Contract

The shared shell guard refuses recognizable fleet `git worktree add` and `git worktree move` commands targeting the integration root or its descendants.  It also refuses fleet `git clone` and `gh repo clone` into that tree unless the destination is the exact registered integration folder for that source repository.  This exception allows onboarding an integration repository without allowing a task clone beside it.

The existing temporary-checkout rules still apply.  Reads, Git fetches, worktree listing and maintenance in existing integration trees remain available.  A denial points to the lane command and the correct repository folder.  Unknown shell expressions, tool errors and hook timeouts retain the existing fail-open behavior; this is a placement guard, not filesystem isolation.

## Server Inventory

The audit found Claude Code, Codex, Muse Code and MiniMax Code installed.  MiniMax was initially missed because its executable was outside `PATH`; checking account-local installation directories found version 0.6.12.  A native server Codex guard already denied integration-tree worktrees and allowed the lanes root, with its own persisted trust.  Keep that registration intact.  Claude had no registered hook, Muse had no installed plugins, and MiniMax had no local fleet guard plugin.

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

Muse's native test fixture wraps the hook payload, rather than taking it at the top level:

```json
{
  "event": "PreToolUse",
  "cwd": "/root",
  "stdin": {
    "tool_name": "bash",
    "hook_event_name": "PreToolUse",
    "tool_input": {
      "command": "false && git -C /root/Code/AI-Fleet-Coordinator worktree add /root/Code/never-create origin/main"
    },
    "cwd": "/root"
  }
}
```

The installed server Claude hook passed integration-deny, temp-deny and lane-allow probes from its persisted Bash registration.  Muse's approved native plugin dispatcher passed the same three cases, recording `blocked` for forbidden destinations and `completed` for the lane destination.  No probe checkout was created.  Claude's unrelated settings and the existing native Codex hook file were byte/content checked unchanged.  A new full Claude session was not started for this proof.

Start a new agent session to load newly installed hooks.  Existing sessions are not interrupted.  Platforms absent from this server require their supported installer target and verification when installed; this rollout does not claim coverage for absent binaries or tools without a hook surface.  Future platform support remains tracked in AFC-353 and AFC-387.

## MiniMax Server Follow-Up

The owner explicitly added MiniMax to this rollout.  Its executable is in the account-local MiniMax Code installation, rather than on the SSH session's `PATH`.  Platform inventory must check known installation directories as well as command lookup.

The installed MiniMax local plugin is `fleet-guard`, using the Claude-compatible manifest and the reviewed shared fleet hook from revision `372aeef8`.  Its `PreToolUse` matcher is **`bash`**, the actual shell tool name.  The hook command uses the compatible deny shape.  The native MiniMax manifest requires an icon, so this shell-only rollout keeps the existing compatible packaging contract.

The installer at this revision still writes the incorrect `Bash` matcher.  Therefore this server bundle was staged with the corrected lowercase matcher, probed with the restricted hook environment, and atomically installed into the previously absent plugin directory.  Do not run that revision's `apply minimax` over this correction.  AFC-365 and PR #712 own the permanent installer correction and the broader MiniMax adapter; this rollout does not replace that work or claim its file-write and seat-identity guarantees.

`mcode plugin list --json` recognizes `fleet-guard@local` as installed and enabled without diagnostics.  Direct subprocess probes deny integration-tree and temporary worktrees and allow the sanctioned lanes destination.  Those commands are payload strings only; the probes never execute Git.  Native dispatcher proof is recorded separately before closeout.  Existing agent sessions remain running and load the new plugin on their next start.
