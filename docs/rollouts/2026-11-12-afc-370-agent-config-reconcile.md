# [AFC-370](https://linear.app/simple-with-us/issue/AFC-370) agent config reconcile

One check-then-apply command compares a host with this checkout and, only with `--apply`, asks the existing installers to repair managed drift.

```bash
python3 scripts/fleet-agent-config reconcile --json
python3 scripts/fleet-agent-config reconcile --json --apply
```

Check is the default.  Drift, a refusal or an error exits 1. `--apply` was not run against a real home in this change.

## What a run compares

- Skills, including the always-on Cursor rule files, through `install-fleet-skills.py`.  Apply uses `--home-only`, so the checkout is not re-rendered.
- Lane Map blocks through `fleet_lanes.install_rules`.  `~/.claude/CLAUDE.md` and `~/AGENTS.md` are report-only.  Apply never passes `--i-own-this-file`.
- Guard hooks through `fleet_lanes.install_tools`.  A platform with no automatic write, including fx, is report-only.  Apply passes `--no-verify`.
- MCP registrations through `fleet_lanes.install_mcp`, without `--with-simple-chat`.
- The fleet-recall MCP entries and Claude hook files that `install-fleet-rag.sh --hooks` owns.  The shell script runs only when one of those managed rows drifts.

A tool home that is not already on the machine is a skip.  The command does not create one.

## Doctor

`fleet-tools-doctor` adds an `agent-config` row from the same check.  The row does not apply.  Drift is `FAIL` / `stale`.  A check that cannot run is `WARN` / `unknown` and the rest of the doctor still finishes.

## Owner steps

1. Review the PR.  This change is Refs [AFC-370](https://linear.app/simple-with-us/issue/AFC-370).  Hosts are not reconciled until check is clean on each of them.
2. On each host, from a checkout of main, run `python3 scripts/fleet-agent-config reconcile --json` and read the rows before any apply.
3. `--apply` writes managed drift through the installers above.  It does not edit the two owner instruction files.  Those stay on `install_rules` with `--i-own-this-file` until that boundary changes.
4. simple-chat MCP registration stays `install_mcp --with-simple-chat`.  Fx guard hooks stay report-only.
5. Do not treat a green doctor row as a substitute for reading the reconciler's own JSON on that host.

## Checks

```bash
cd scripts && python3 -m unittest fleet_agent_config.tests.test_reconcile -v
cd scripts && python3 -m unittest test_fleet_skill_identity.InstallerRepoOnlyTests test_fleet_skill_identity.AlwaysOnRuleFileTests -v
```

The fx apply test uses a temporary home only.
