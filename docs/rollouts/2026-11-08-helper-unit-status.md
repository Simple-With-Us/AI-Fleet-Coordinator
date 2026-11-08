# Unit Status, Logs Of The Service A Timer Starts, And The Redeploy Name

Muster's Server panel needs four small things from the restricted helper (`scripts/fleet-ops-control.py`, the forced command of the `fleet-ops-ctl` key) that it cannot get today: a way to read one listed unit's state on demand, the journal of the service a timer starts instead of the timer's own two-line journal, the authoritative name of an application before a redeploy is approved, and the owner copy for the Mac's agents that the server's units already carry.  This note describes the four changes, the exact shape of each answer for the Swift owner, how to deploy them, and what was checked.

Nothing here changes the installer, sudoers, SSH, any unit or any setting, and nothing here is offered on the `--from-muster` route.  That route is inert today (every approval is `no_trust_list`), and its verb table is the design owner's to change.

## 1. The New Verb: `server unit-status UNIT --plan --json`

A read of one unit the allowlist lists, with one fixed argv and nothing from the request in it but the unit name: `systemctl [--user] show --timestamp=unix --property=<properties> <unit>`.  The scope (`--user` or none) comes from the allowlist entry.  There is no sudo.  The call goes through the helper's own bounded runner (`ctx.runner`, the same one `update-status` uses) with the pinned child environment (`child_env`, which keeps `XDG_RUNTIME_DIR` so the user manager answers), a 10 second limit and a capture of at most 4097 bytes.

- **Which units.**  An entry of the allowlist's server `units` or of its `watch` list.  The scope is the entry's.  A unit in both lists keeps its `units` entry.  Reading a watched unit grants it nothing: `start`, `stop`, `restart` and `logs` still refuse it (`allowlist_miss`), as the server-status collector's own tests require.  Until now the helper never read `watch`; it now reads it, and validates it (below).
- **Plan only.**  `--yes` is refused with `errorCode` `read_only_required` and exit status 2, and the refused attempt is audited like every refused `--yes` (one `refused` row).  `update-status` and `resource-status` leave no row for a refused `--yes`; this verb audits it, as `seat-inventory` and `eavesdropper-status` do.  A plan takes no action lock, asks for no sudo and writes no audit row.
- **Not on the `--from-muster` route.**  `scripts/muster_approval.py` keeps `READ_VERBS = ("resource-status", "seat-inventory", "update-status")`, so `server unit-status ...` over that route is `bad_command` before standard input is read (new tests pin that).  Offering it there needs a spec change first.

### The Answer

```json
{
  "ok": true,
  "verb": "server unit-status",
  "target": "fleet-server-status.timer",
  "commands": [],
  "result": "planned",
  "detail": "read-only status of a listed unit",
  "unit": "fleet-server-status.timer",
  "scope": "user",
  "activeState": "active",
  "subState": "waiting",
  "unitResult": "success",
  "lastExitAt": null,
  "nextElapseAt": "2026-11-08T08:45:00Z",
  "checkedAt": "2026-11-08T08:37:12Z"
}
```

The fields sit at the top of the answer, as `update-status`'s do, next to the usual envelope (`ok`, `verb`, `target`, `commands`, `result`, `detail`).  `commands` is always `[]`.

| Field | Type | Meaning |
| --- | --- | --- |
| `unit`, `scope` | string | The unit asked for and the scope the allowlist entry gave it: `user` or `system`. |
| `activeState` | string | systemd's `ActiveState` word: `active`, `inactive`, `failed`, `activating`, `deactivating`, `reloading`, `maintenance`. |
| `subState` | string | systemd's `SubState` word, for example `waiting`, `running`, `dead`, `exited`, `failed`, `start-pre`, `auto-restart`. |
| `unitResult` | string or null | systemd's `Result` word for the unit (`success`, `exit-code`, `signal`, `timeout`, `start-limit-hit`, ...), null when it is empty.  It is named `unitResult` because `result` is the answer's own word. |
| `lastExitAt` | string or null | `ExecMainExitTimestamp` as `YYYY-MM-DDTHH:MM:SSZ` in UTC, null when the property is empty, `n/a` or 0.  systemd clears it when the main process starts again, so **a service that is running now reads null, however many times it has exited before**: on the server `docker.service` and `tailscaled.service` (never restarted) and `claude-remote-control.service` (restarted twice, running) all read it empty, so it is always null for the two watched services.  **Always null for a `.timer`**: a timer has no main process, systemd has no such property on it, and the reader does not ask for one.  Read the service the timer starts for its last run. |
| `nextElapseAt` | string or null | **Only on a `.timer`** (the key is absent for a service).  `NextElapseUSecRealtime` in the same form, null when the timer has no realtime elapse.  A timer that fires by a monotonic clock (`OnBootSec`, `OnUnitActiveSec`) has none: five of the six timers on the allowlist (`code-main-sync`, `fleet-tools-doctor`, `fleet-housekeep`, `box-disk-hygiene` and `fleet-cli-git-update`, read on the server on Nov 8) answer null, and only `fleet-server-status.timer` (`OnCalendar`) answers a time. |
| `checkedAt` | string | The helper's own clock when it answered, `YYYY-MM-DDTHH:MM:SSZ` in UTC. |

Every string is a lowercase systemd word of at most 32 characters (`[a-z][a-z0-9-]{0,31}`) or one of the times above.  Nothing else of systemd's output reaches the answer.

### When It Cannot Answer

The failures mirror `update-status`: the answer is a refusal (`ok` false, `result` `refused`, exit status 2) with a fixed `detail` sentence and an `errorCode` word, and nothing of systemd's own text.

| `errorCode` | When |
| --- | --- |
| `read_only_required` | `--yes` was passed. |
| `unit_status_unavailable` | `systemctl` exited non-zero, timed out or could not start. |
| `unit_unavailable` | `LoadState` is `not-found`, `masked`, `error` or `bad-setting`. |
| `unit_status_invalid` | The output is not what the reader accepts (below), or `LoadState` is something else than `loaded`. |
| none (`detail` "the unit is not in the allowlist") | The unit is not an entry of `units` or `watch`, exactly as `restart` of an unlisted unit refuses. |
| none (`detail` "the unit is not a valid unit name") | The name is not `[A-Za-z0-9][A-Za-z0-9_.-]{0,120}` plus `.service` or `.timer`. |

### What The Reader Accepts

The output of `systemctl show` for the asked properties, one `KEY=VALUE` line each, in any order, and nothing else: ASCII only, newline separated, no other key, no repeated key, no blank line, at most 4096 bytes, each state word matching the pattern above and each time being `@SECONDS` (what `--timestamp=unix` prints; the server collector uses the same flag) or an unset word (empty, `n/a`, `0`).  A carriage return, a byte outside ASCII, a NUL, a second unit's block, an unknown key or a value past its pattern is `unit_status_invalid`, and none of it is repeated in the answer.  A time that is not `@` and digits, or is past the year 9999, is `unit_status_invalid` too (an unset time is null, never invalid).

What `systemctl show --timestamp=unix` printed on the server on Sunday, November 8, 2026, read-only, as the test data:

| Unit | Lines, in the order systemd printed them (`TIMER_SHOW`, `SERVICE_SHOW`, `RUNNING_SHOW` and `MONOTONIC_TIMER_SHOW` in the tests) |
| --- | --- |
| `fleet-server-status.timer` (user) | `LoadState=loaded`, `ActiveState=active`, `SubState=waiting`, `NextElapseUSecRealtime=@1794127500`, `Result=success` |
| `fleet-server-status.service` (user) | `LoadState=loaded`, `ActiveState=inactive`, `SubState=dead`, `Result=success`, `ExecMainExitTimestamp=@1794126615` |
| `docker.service` (system) | `LoadState=loaded`, `ActiveState=active`, `SubState=running`, `Result=success`, `ExecMainExitTimestamp=` (empty: it is running, and systemd clears it while the main process runs) |
| `fleet-cli-git-update.timer` (system) | `LoadState=loaded`, `ActiveState=active`, `SubState=waiting`, `NextElapseUSecRealtime=` (empty: a monotonic timer), `Result=success` |

## 2. `server logs` Reads The Service A Timer Starts

`journalctl -u <timer>` shows only `Started` and `Stopped`.  The journal worth reading is the service's.  An allowlist entry in `server.units` may now carry `logsUnit`, a unit name of the same safe shape as `unit` (read in the entry's own scope).  Without one, a `.timer` reads the same name with `.service`, and any other unit reads itself.  A bad `logsUnit` (not a string, not a unit name, a path, a template, another suffix) refuses the whole file, like every other bad key.

No entry of the repo allowlist needs one.  The default is right for both timers, and the test suite checks it against the shipped unit files: `fleet-server-status.timer` reads `fleet-server-status.service` (a user unit) and `fleet-cli-git-update.timer` reads `fleet-cli-git-update.service` (a system unit), neither timer has a `Unit=` line, and both services exist on the server (read-only `systemctl show`: `LoadState=loaded`, `Type=oneshot`, in the scope the entry names).  Read-only on the server on Nov 8, `journalctl --user -u fleet-server-status.timer -n 200 --no-pager` printed `-- No entries --`, and the same read of `fleet-server-status.service` printed a full 200 lines.

**The planned argv of `logs` changes for a timer, on both routes.**  `server_logs_commands` is shared, so the plan of `server logs fleet-server-status.timer` is now `[journalctl, --user, -u, fleet-server-status.service, -n, 200, --no-pager]`, not `... -u fleet-server-status.timer ...`.  The signed plan's `target` and `auditTarget` stay the timer, so the plan hash of a timer's logs plan changes: for `fleet-server-status.timer` in the test fixture `scripts/fixtures/muster-vectors-v5.json` it moved from `f568a2a74be62d654cd23657903fbec952a451bfc1778031235d53a561ed7a87` to `1ff02a672f25fa559b0307b2744f694c1fac4426263b4a9f2987f6b0a6d088c4` (the fixture is regenerated by `scripts/make_muster_vectors.py --write`, and only its `logsResult` entry changed).  A logs plan that Muster signed before the helper was replaced no longer hashes to what the helper plans, so it is refused with `plan_mismatch` at the run and the user plans again.  The plan hash of a non-timer's logs plan, and of every other verb, is unchanged.

**The `--from-ssh` answer** gains one field, for both a plan and a `--yes` run (and for a run whose journal read failed):

```json
{"ok": true, "verb": "server logs", "target": "fleet-server-status.timer",
 "commands": [["/usr/bin/journalctl", "--user", "-u", "fleet-server-status.service", "-n", "200", "--no-pager"]],
 "result": "planned", "detail": "last 200 journal lines, read only",
 "logsUnit": "fleet-server-status.service"}
```

`logsUnit` is the unit whose journal is read: the timer's service, the entry's own `logsUnit`, or the unit itself.  The signed `--from-muster` plan has no such field (its shape is fixed by the spec), and the unit is visible in its `commands`.  The audit row of a logs run keeps the requested unit as its target and the line count, as before, and no log text.  No other verb's answer carries `logsUnit`.

## 3. The Redeploy Name

`server redeploy UUID --plan|--yes` answers with a new field, `resourceName`, in the plan answer and again in the `--yes` answer (and in a `--yes` answer that failed after the plan):

```json
{"ok": true, "verb": "server redeploy", "target": "<uuid>",
 "commands": [["coolify", "GET", "/api/v1/deploy?uuid=<uuid>"]],
 "result": "planned", "detail": "redeploy", "resourceName": "fleet-recall"}
```

It is the string the audit row records as its target, made printable: the `name` of the Coolify row in the status report, with a trailing ` (SERVICE)` removed when the row's `service` is that service, cut to 120 characters, and then every code point outside `0x20` to `0x7E` replaced by one `?` (one `?` per code point, no collapsing, no trimming, so `Café App` is `Caf? App` and the length never grows).  The cut comes first, so a name that straddles 120 characters is cut before it is made printable.  The audit row keeps the real name and the answer carries the printable form: for `Café App` the audit row says `Café App` and `resourceName` says `Caf? App`.  A name that is not a name is the placeholder `coolify resource`, in the answer and in the audit row alike: a missing name, an empty or blank one (only whitespace, which is also what a service-only name is once its service is taken off), and one equal to the uuid.  (Before this change the audit row recorded an empty string for an empty name; it now records the placeholder.)  The signed `--from-muster` plan is not changed by any of this: its `auditTarget` is the planner's own value made printable, so it equals `resourceName` for every row that has a usable name, and for a blank or uuid-equal name it keeps what the planner returned (empty, whitespace, or the uuid) where the answer and the audit row say `coolify resource`.  A refused redeploy (no row, a stale report, an application not running) has no `resourceName`.

Muster can show it as the authoritative name, put it in the Touch ID reason, and refuse before the yes step when it differs from the name it had.  No argv word is added, and the signed `--from-muster` plan is unchanged.

## 4. Owner Copy For The Mac's Agents

`role` and `stopConsequence` were server-unit keys that only the collector read; the helper ignored them.  They are now also on Mac agents, and the helper checks them on every entry kind (Mac agents, Mac pm2 applications, server units): `role` must be `reporting` or `updater` (the collector's own list, `UNIT_ROLES` there and here), and `stopConsequence` must be one line of printable ASCII of 1 to 200 characters with no space at either end.  A bad value refuses the whole file, like any other malformed entry, so it never reaches Muster.  The helper acts on neither key and does not keep them.  **Write `stopConsequence` as plain ASCII with ordinary spaces.**  The fleet's file rule of a real U+00A0 plus a space between sentences does not apply to this one field, because a character outside printable ASCII (a no-break space included) makes the helper refuse the whole allowlist, every Mac and server verb with it.  Each text is one sentence, so it needs no gap.  The installer checks only `schemaVersion`, so nothing at install time would catch a bad value; `AllowlistNotesTests` does, on the repo file, and the comment beside `STOP_CONSEQUENCE_RE` in the helper says the same.

No action changed: every agent below keeps `start`, `stop` and `restart`.

| Label | `role` | `stopConsequence` |
| --- | --- | --- |
| `com.jay.fleet-mac-services` | `reporting` | Stopping this stops the Mac's agent report, so Muster's Services panel goes out of date and the Mac's jobs read as not known. |
| `com.jay.fleet-tools-doctor` | `reporting` | Stopping this stops the fleet tools report, so Muster's Fleet Tools rows go out of date and then read as not known. |
| `com.jay.fleet-worker-health` | `reporting` | Stopping this stops the worker health report, so Muster's worker card goes out of date. |
| `com.jay.simple-tools-runtime-sync` | `updater` | Stopping this stops the Mac's fleet tools updating from main, and the Operator listener is no longer restarted when its code changes. |
| `com.jay.operator-notes-runner` | none | Stopping this stops the Mac answering Apple Notes requests from the server and cloud seats, and they wait in the Operator's queue for up to 24 hours. |

The notes runner's sentence was written from what the runner does (`scripts/launchd/com.jay.operator-notes-runner.plist`, `scripts/agent_sync/notes_runner.py`, `docs/protocols/operator-notes.md`): it long-polls the Operator for Apple Notes requests that the server's CLIs and the hosted tools send, runs `apple-notes-coding.sh` for each and reports the result, and a request nobody claims within 24 hours expires.  It has no `role` on purpose.  Neither word the allowlist uses describes it (`reporting` means a unit that keeps Muster's own report current, `updater` one that keeps the fleet tools current), and a wrong word would put it in the wrong group; a third word needs the Swift owner's agreement first.

The `watch` list is read now, so it is validated: every entry must be an object with a valid unit name and a `user` or `system` scope, or the whole file is refused.  Extra keys on a watch entry (an `actions` key, say) are still ignored and still grant nothing, and a unit named in both lists keeps its `units` entry.  The same name in two scopes across the lists is refused, because a status request names a unit and no scope.

Every list in the file (`mac.agents`, `mac.pm2Apps`, `server.units`, `server.watch` and `server.updates`) must be a list.  A number, a flag, a string or an object in its place is refused as `... is not a list`, in the loader's usual JSON refusal (exit 2); before, such a value was a traceback with no JSON that took every verb down.  An absent or empty value (missing, `null`, `0`, `false`, `""`, `{}` or `[]`) is an empty list, as `or []` always read it and as Muster's reader of the file does.

The server-status collector reads the same file on its own and is more forgiving than the helper: it skips a watch row it cannot read, a `role` outside its list and a `stopConsequence` that is too long, where the helper refuses the whole file for any of them.  When the helper refuses, the collector still lists the units and puts `allowlist_refused` in its `collection` block, but no unit is `controllable` and `can_redeploy` is false.  The collector's role words and 200-character limit are tested equal to the helper's (`AllowlistNotesTests`), so the two cannot drift apart unnoticed.

## Deployment

The server CLI checkout update does not replace the protected broker installation.  This change replaces two files under `/usr/local/lib/fleet-resource-control`, the way `2026-11-07-eavesdropper-status-verb.md` did: a hash-verified backup of the installed `fleet-ops-control.py` and `fleet-ops-control.json`, each new file written beside its target as a candidate, its sha256 checked against the repo file, and an atomic rename for each.

- **The JSON and the helper go in together**, in one deploy, JSON first and helper second.  Every state between the two renames works: the old helper ignores the new keys (`role` and `stopConsequence` on Mac entries are not read, there is no `logsUnit`), and the new helper with the old JSON derives the default `logsUnit` and reads the `watch` list the Nov 7 deploy brought in.  The review tried all four pairings of the old and new helper with the old and new JSON (the old helper with the new JSON says `unknown server verb` for `unit-status` and plans everything else), so helper-then-JSON is equally safe, and so is a rollback in either order; JSON first is the order this note uses.
- **What is installed now, and what this swap also ships.**  The installed helper (sha256 `5cb19c93dca8a024921fbf8825c3c5b24a29fa7b27ba6e981efc803d3f934662`) and the installed JSON (`241482d3f0fcd2ed2012f6c1e9c848ffd88b92cdf4deabac0e0909cc9bd8dcde`) are the files of commit `927fd6eea` (#1479, installed on Nov 7).  `main` has moved since: #1505 (`078965fe5`) added the `--from-muster` entry to the helper, changed `fleet_resource_control.py`, and added `muster_approval.py` and `muster_agent/muster_trust_files.py`, and **none of that was ever installed**.  So this swap also ships the `--from-muster` entry code in the helper, beside the OLDER installed `fleet_resource_control.py` and WITHOUT the two muster modules.  That mixed layout is safe, and an independent review checked it: every `--from-ssh` verb works, and `--from-muster` answers the fixed unsigned refusal `helper_key_missing` (exit 2, no traceback), because the modules it needs are not there; the same two results were reproduced in a scratch directory that held only the new helper and JSON.  (`main`'s `reduced_status.py` also differs from the installed copy, in a docstring only (#1462), and nothing here installs it.)  Nothing else is replaced: do not run the full installer, and do not change sudoers, SSH, units or settings.
- **The Mac** runs the helper and reads the allowlist from its own checkout of `scripts/` (`OpsControl.scriptPath` and `allowlistPath` in Muster), so it has the new files when that checkout does; nothing is installed by hand there.
- **Ship the Muster build in either order.**  A build that asks for `unit-status` before the helper has it gets the helper's refusal ("unknown server verb"); a helper that has it before the build asks does nothing.  A Muster build that reads `resourceName` or `logsUnit` must treat a missing field as an older helper.
- **A plan signed before the swap.**  A logs plan for a timer, signed with the old argv, is refused at the run with `plan_mismatch` (the route is inert today, so nothing is in flight).

Rollback restores the two backups through the same candidate-and-rename procedure, helper first, then the JSON (either order works).

## Checks

- `python3 scripts/test_fleet_ops_control.py` (new: `UnitStatusTests`, which pins the zone to US Central so that a time converted in local time fails on a UTC runner too, `AllowlistListShapeTests`, `AllowlistNotesTests`, `LogsUnitTests`, `WatchListTests` and `RedeployResourceNameTests`), `python3 scripts/test_muster_approval.py` (the logs plans and the vector fixture moved with the argv; `unit-status` is `bad_command` on the route), `python3 scripts/test_fleet_resource_control.py`, `python3 scripts/test_fleet_server_status.py`, `python3 scripts/test_fleet_cli_release.py`, `python3 scripts/test_fleet_skill_identity.py`, and `python3 scripts/make_muster_vectors.py --check`.
- The installer tests at umask 022: `(umask 022; python3 -m unittest discover -s scripts/hetzner -p test_install_resource_control.py)`.
- After deployment, through the Mac's real route (`ssh fleet-ops-ctl -- server ... --plan --json`): `unit-status fleet-server-status.timer` is `planned` with `scope` `user` and a `nextElapseAt` in the next 15 minutes; `unit-status fleet-cli-git-update.timer` is `planned` with `scope` `system` and `nextElapseAt` null (a monotonic timer); `unit-status docker.service` has no `nextElapseAt`; `unit-status sshd.service` and `unit-status fleet-server-status.service` are `refused` (not listed); `unit-status docker.service --yes` is `refused` with `read_only_required` and leaves one `refused` audit row.
- `logs fleet-server-status.timer --plan` shows `-u fleet-server-status.service` and `logsUnit`, and `--yes` returns the service's journal lines rather than the timer's two.
- `redeploy <uuid> --plan` for a live application returns `resourceName` equal to the name in Muster's Server panel (a plan changes nothing; do not run `--yes` to try it).
- The installed allowlist has the sha256 of the repo file, names `com.jay.simple-tools-runtime-sync` once, and `grep -c agent-sync` on it prints 0.
- The mixed layout, as the installed helper will run it: `env -i HOME=/home/jay SSH_ORIGINAL_COMMAND='muster trust-list current --plan' /usr/bin/python3 -I /usr/local/lib/fleet-resource-control/fleet-ops-control.py --from-muster` still prints the unsigned refusal with `"errorCode":"helper_key_missing"` and exits 2, with no traceback (the route stays inert until the muster modules are installed); `ssh fleet-ops-ctl -- server resource-status coding --plan --json` still plans.
- `python3 scripts/test_fleet_cli_release.py`: the fleet CLI release ships `agent_sync/` under a 155-file receiver budget, and this change adds no file to it or to `scripts/` (the code is an edit of the helper).

## Open Items

- `docs/muster-ios/server-route-spec.md` section 4 and `docs/muster-ios/design.md` still print the `logs` plan as `journalctl ... -u UNIT ...`: for a timer `UNIT` is now the entry's `logsUnit` (the service).  Those documents are the design owner's, so they are not edited here.
- `unit-status` is not in the spec's verb table, so `--from-muster` answers `bad_command` for it.  Adding it is a spec change first, then one tuple entry in `muster_approval.py` and one in `_MusterBackend.target_ok`.
- On this allowlist `lastExitAt` is always null: for a `.timer`, and for a running service, which is what `docker.service` and `tailscaled.service` are.  `nextElapseAt` is null for five of the six timers.  If Muster needs a timer's last run, the service it starts has to be listed (in `watch`, say) so that `unit-status` can read its `lastExitAt` while that service is not running; adding a service to `watch` also adds a row for it to the server-status report, so that is a separate decision.  A timer's own `LastTriggerUSec` would fill a last-run field cheaply, and is not read.
- Muster's own reader of the allowlist (`OpsAllowlist.parse` in `OpsControl.swift`) mirrors the structural refusals of `validate_config` and does not yet repeat the new ones (a bad `role`, `stopConsequence` or `logsUnit`, or a malformed `watch` entry).  Where the helper refuses the file, Muster would still draw buttons that fail, so the Swift owner may want to mirror them.

## Deployed

Not deployed yet.
