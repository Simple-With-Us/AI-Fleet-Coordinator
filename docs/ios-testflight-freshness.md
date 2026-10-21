# TestFlight freshness deploy check

Owner request, Sat, Oct 17, 2026:  bring every TestFlight app up to date, add deploy checks,
and make the apps update automatically from now on.  Linear AFC-341.  The skill and docs side
(the deploy-verify iOS section) is AFC-340.

One shared check answers one question for every fleet app:  **does the newest processed
TestFlight build contain the newest `main` commit that touches the app's native tree?**  With
`--ship-behind` it also starts the app's own ship workflow when the answer is no, under the
fleet's TestFlight guards.

## Files

| File | What it is |
|---|---|
| `scripts/ios-fleet/ios_testflight_freshness.mjs` | The check, the remedy and the alerts.  Node, no dependencies. |
| `scripts/ios-fleet/testflight-apps.json` | The registry:  one row per App Store Connect record.  **Edit this** when an app, a repo path, a ship workflow or a compile check changes. |
| `scripts/ios-fleet/ios_testflight_freshness.test.mjs` | Tests (`node --test`, everything stubbed, run in CI). |
| `scripts/ios-fleet/install-testflight-freshness.sh` | Copies the check and the registry into `~/apps/ios-fleet` and loads the LaunchAgent. |
| `scripts/launchd/com.jay.ios-testflight-freshness.plist` | The LaunchAgent:  every two hours at :20. |

`~/apps/ios-fleet` stays the install target, as for the rest of the fleet's TestFlight tooling.
The check calls `~/apps/ios-fleet/asc-api.mjs` for App Store Connect (that script takes the
`ASC_*` env or Infisical `/asc` through `fleet-secrets`; the check never reads a credential and never echoes
`asc-api.mjs` stderr, because a crash there can name the key path) and
`~/apps/ios-fleet/build-window.sh` for the build window.  Set `IOS_FLEET_DIR` to point both
somewhere else.

## Commands

```bash
node ~/apps/ios-fleet/ios_testflight_freshness.mjs                  # table for every app
node ~/apps/ios-fleet/ios_testflight_freshness.mjs --app botfleet   # one app
node ~/apps/ios-fleet/ios_testflight_freshness.mjs --repo BotFleet  # every app a repo ships
node ~/apps/ios-fleet/ios_testflight_freshness.mjs --json           # machine readable report
node ~/apps/ios-fleet/ios_testflight_freshness.mjs --ship-behind --dry-run   # the catch-up plan, sends nothing
node ~/apps/ios-fleet/ios_testflight_freshness.mjs --ship-behind --alert     # what the LaunchAgent runs
```

From a lane, `node scripts/ios-fleet/ios_testflight_freshness.mjs` works the same way (it finds
`asc-api.mjs` in `~/apps/ios-fleet`).  A full run takes about 20 seconds.

Every App Store Connect call is one `asc-api.mjs` child, and each child signs in to Infisical
through `fleet-secrets` (three `get`s, each a login plus an export).  The check runs at most two
children at once, retries a child that was killed by its 120 second timeout, and stops spawning
after three kills in a row.  A report line that says `asc-api.mjs killed by SIGTERM after the 120s
timeout, host load N` means the Mac was overloaded (the 2026-10-20 2:20pm and 4:20pm runs failed
this way at load 100 to 200 with memory thrashing), not that App Store Connect or the credentials
are wrong:  find what is saturating the Mac (`top -o cpu`, the Housekeeper) and re-run.  A real
credential problem reads `HTTP 401` or `Missing ASC_KEY_ID`.  Before 2026-10-20 a killed child
was printed as `asc-api.mjs exit ?`.

Exit codes:  **0** every checked app is current;  **1** something is behind, or a catch-up ship is
still running or processing;  **2** the check itself failed (App Store Connect or GitHub
unreachable, an unknown `--app`, a registry row that matches no commit).

A full run also writes the report to `~/.cache/ios-fleet/freshness-last.json`.

## How "shipped" is decided

App Store Connect never records a git sha, so the check reconstructs it:

1. **The ship run.**  The build number is the repo commit count, so a current build carries no cut
   time.  The ship workflow run whose start-to-end window contains the upload is the run that
   uploaded the build, and its `head_sha` is the shipped source.  The run's conclusion is ignored on
   purpose:  most green ship runs are gate skips that upload nothing, and BotFleet 1.0.113 was
   uploaded by a run that went red afterwards.  A build uploaded under the retired scheme still
   carries a 12-digit UTC stamp, and those are correlated by their cut time instead.
2. **The state file** `~/.cache/ios-fleet/last-ship-<app>.txt`, when its time matches the upload.
   Hosted runners do not write it here, so this mostly covers local ships.
3. **Time**, labelled `time` in the output:  the build's upload time against the commit time.

Containment uses the compare API:  `ahead` or `identical` from the newest native commit to the
shipped sha means current.  The table's SHIPPED column says which source was used.

## Statuses

| Status | Meaning |
|---|---|
| `current` | The newest processed build contains the newest native commit. |
| `BEHIND` | It does not.  The NOTE column says which commit is missing and what the remedy decided. |
| `SHIPPING` | A ship run that contains the commit is queued or running, and started under 3 hours ago (`inflightMaxHours`).  An older run counts as stuck:  the app shows `BEHIND`, the NOTE links the stuck run, and it no longer holds back other apps. |
| `PROCESSING` | An upload after the commit is still processing in App Store Connect. |
| `NO-BUILD` | The App Store Connect record has no TestFlight build at all. |
| `EXPIRED` | Every build has expired. |
| `skipped` | Listed in the registry with a `skip` reason (the Mac apps that ship through Sparkle or GitHub releases). |
| `ERROR` | The check could not evaluate this app.  Counts toward exit 2. |

The COMPILE column is the app's iOS compile check (`compileCheck` in the registry) on main HEAD,
or on the newest native commit when main HEAD has none because of a path filter.  `pending` and
`unknown` are not green.  GATE is `open` once 2.5 hours have passed since the app's last upload,
measured from App Store Connect.  Apps in App Store Connect that the registry does not list are
named under the table, so a new app cannot go unchecked.

## The automatic remedy (`--ship-behind`)

For each app that is behind, oldest gap first, the check runs the app's **own** ship workflow
with `gh workflow run <workflow> --repo Simple-With-Us/<Repo> --ref main` (plus the registry's
inputs, for example `-f upload=true` for Hog Hunter).  It never runs
`xcodebuild` here, never passes `--force-ship`, never sets `IOS_TF_MIN_INTERVAL_SEC`, and never
runs an App Store GM workflow.  Every one of these must hold, or the app is reported
`blocked` or `deferred` with the reason:

1. `autoRemedy` is on for the app (the registry refuses to load when an app with `autoRemedy` on names no `compileCheck`), and its ship workflow exists and is `active` (a
   `disabled_manually` workflow is the owner's switch;  it is reported, never re-enabled).
2. The compile check on main is green.  A ship job gated on a repo variable (`ship.enabledVar`, Hog Hunter's `HH_IOS_TESTFLIGHT_ENABLED`) is only dispatched while that variable is `true`;  otherwise the dispatch would be a skipped job that uploads nothing.
3. 2.5 hours have passed since the app's last upload.  The check enforces the documented fleet
   gate itself, because ContactLogo does not ship through `ship-testflight.sh`.  Hog Hunter does (HH-157), and its `paths` here are the same exact files as its workflow's push filter.
4. The app has not already been dispatched for this main HEAD.  A ship that keeps failing is
   tried once per HEAD, not every two hours;  a new main commit re-arms it.  (The same-HEAD skip
   inside `ship-testflight.sh` still applies on the runner.)
5. `build-window.sh <app>` does not say `background`.  The LaunchAgent fires at :20, inside
   Socratic Trade's X:15-X:45 window.
6. No ship run of any registry app is queued or running, even when `--app` narrowed the check.  A run older than 3 hours is stuck and does not count.
7. At most `--max-ships` (default 1) dispatches per run.

`--dry-run` prints the plan (`would-dispatch` and the exact command) and writes nothing.

## Alerts (`--alert`)

The check posts to Zulip `#agent-sync`, topic `AFC testflight-freshness`, with
`agent-sync post --as CLAUDE --session tfresh` (override the seat with `IOS_TF_FRESHNESS_SEAT`):

- an app the check has seen behind, without a break, for **more than 6 hours**:  by then the
  catch-up ship has had its chance, or the line says why it could not run (pipeline disabled,
  compile red, manual only, a ship run that looks stuck, or a catch-up that was dispatched and never landed).  A catch-up gets 3 hours (`remedyLandHours`) to archive, upload and process before an alert may call it a failure, so the run that dispatches it never alerts about it;
- the check itself failing (exit 2).

Never spam:  one digest post per run, at most one post per app per 6 hours, and never twice for
the same commit and reason.  A disabled pipeline therefore alerts once per new native commit,
not every run.  A failed post does not change the exit code;  its stamps are rolled back so the
next run retries.  State lives in `~/.cache/ios-fleet/freshness-state.json` (mode 600, written
atomically), and a lock directory beside it keeps two mutating runs apart.

## Where it runs, and why there

A Mac LaunchAgent, `com.jay.ios-testflight-freshness`, every two hours at :20 (scheduled, not
always-on;  registered in `MAC-LOCAL-PROCESSES.md`).  It needs **no new secret**:  App Store
Connect credentials are already in Infisical (`/asc`, read with `fleet-secrets get`) for `asc-api.mjs`, `gh` is signed in with the
`workflow` scope that `workflow_dispatch` needs, and `agent-sync` has the CLAUDE bot's Zulip
credential, all verified under a launchd-style clean environment.  A GitHub Actions cron in
AI-Fleet-Coordinator could read the App Store Connect key through the org's Fleet-OPS Infisical
identity (ContactLogo's workflows already do), but it would still need a token proven to run
`workflow_dispatch` in every app repo (AFC's `FLEET_GITHUB_TOKEN` has an unverified scope) and a
Zulip bot credential for the alert, which AFC does not hold.  The job is a few dozen API calls
with no build, so it adds no real load to the shared Mac;  the ships it starts run on each app's
own runner.

## Post-merge deploy check

After a merge that touches an app's native tree, the deploy is verified when
`node ~/apps/ios-fleet/ios_testflight_freshness.mjs --app <key>` (or `--repo <Repo>`) exits 0.
`SHIPPING` or `PROCESSING` means wait and re-run;  `BEHIND` means read the NOTE:  the ship run,
the runner, the rate gate, or the compile check.  The post-merge check is report-only:  it is not
run with `--ship-behind`, because the LaunchAgent owns catch-up ships and the pr-sweep deploy
rules forbid an iOS ship from a sweep.

## Install

From an AI-Fleet-Coordinator lane at origin/main:

```bash
bash scripts/ios-fleet/install-testflight-freshness.sh --dry-run
bash scripts/ios-fleet/install-testflight-freshness.sh
```

It keeps a replaced file as `<file>.bak-freshness-<stamp>`.  Logs:
`~/Library/Logs/ios-testflight-freshness.log` and `.err.log`.

## First real run (Sat, Oct 17, 2026, 7:37pm)

3 current (Socratic Trade 1.0.111, BotFleet 1.0.113, Hog Hunter 1.0.6), 2 shipping (Usage
Monitor Client and Local, a ship run in flight), 7 behind, 2 skipped (ContactLogo for Mac,
BotFleet for Mac), every App Store Connect app mapped.  Behind:  Congress.Trade 1.0.235 and
DealDex 1.0.316 (both `ios-ship.yml` disabled_manually), ContactLogo 1.0.3, Clutch.Codes 1.0.3,
CodeCaps 0.1.84, FleetLink 2.0.0, and CodeCaps for Mac 0.1.12 (manual-only workflow).  The dry
run deferred every catch-up because the Usage Monitor ship was in flight:  one ship at a time.

## Known gaps the check reports but does not fix

- `ios-ship.yml` is `disabled_manually` in Congress.Trade and DealDex.  Re-enabling is the
  owner's call;  until then those apps alert once per new native commit.
- DealDex has no iOS compile check on main (its `ci.yml` builds the web app only), so `autoRemedy` is off for it:  a catch-up needs a green compile check.  Add an iOS compile job, name it in the registry, and turn `autoRemedy` on.
- CodeCaps for Mac is retired from TestFlight and the Mac App Store (CC-169, owner
  2026-10-19), so the registry carries a `skip` reason rather than a ship workflow:
  the Mac app ships through GitHub release DMGs.  It reports as `skipped`, not
  `BEHIND`, so there is nothing to catch up.
- The live `ship-testflight.sh` sets `DEFAULT_MIN_INTERVAL_SEC=3600` (1 hour, owner
  2026-08-14) while the ios-fleet README documents 2.5 hours.  The check uses 2.5 hours, the
  stricter of the two.  Which one is the standing limit is the owner's call.
