# Muster

Muster is the fleet owner's native SwiftUI macOS 14+ control app.  It shows what runs on the Mac and what runs on the server as two named places, and it acts on each through a route that names the machine.

## Every Panel

Every place in the window's sidebar has one row here, and `MusterTests/ReadmeSurfaceTests.swift` fails when a place in `MusterDestination` (`Muster/Model/MusterNavigation.swift`) has no row or a row names no place.  About says which machine the panel's data describes.  The last column says what the panel can change and by which route.

A route marked **plan and Touch ID** runs the helper with `--plan` first, which changes nothing, shows that plan in a sheet, asks Touch ID, plans again and refuses if the plan changed, and only then runs `--yes`.  One such action runs at a time, app-wide.  Everything that changes the server goes over `/usr/bin/ssh` to the `fleet-ops-ctl` alias, whose forced command on the server runs `scripts/fleet-ops-control.py --from-ssh` and checks the allowlist again;  the server's helper writes its own audit row there, and Muster adds one marked `"via": "muster"` to the Mac's `~/.local/state/fleet-tools/ops-audit.jsonl`.  The allowlist is `scripts/fleet-ops-control.json`.

| Panel | About | Data From | How Fresh | Can Change, And The Route |
| --- | --- | --- | --- | --- |
| Overview | The Mac and the server | The menu's own summary of each place, for The Mac and The Server cards, then the last engine run's cells, the same cells Compliance Grid paints, and Drift History's count | Each engine run;  a run that could not be trusted leaves every count unknown, never zero | Nothing;  each card opens its panel |
| Compliance Grid | The Mac:  every rule in `fleet-seats.json` is collected on the Mac today | `/usr/bin/python3 -m fleet_config status --json --no-doctor`, run on the Mac from the repository's `scripts/` against `fleet-seats.json` and `fleet-apps.json` | The engine runs at launch, when a watched file changes, every ten minutes and on the toolbar's Refresh;  each cell shows its own age | A red cell's fix (see Apply Preview):  `fleet_config apply SEAT KIND --plan`, one confirmation, then `--yes`, on the Mac;  Touch ID for persistence-class writes is inside the engine |
| Apps & Work | Across the fleet | `fleet-view status --json` (`scripts/fleet_view/`, run with `/usr/bin/python3` on the Mac), which reads `fleet-apps.json`, git in each lane, `gh` and `simple-board list`, plus the doctor's `report.json` | On open and on Refresh;  it shows the collector's own time, and a failed refresh clears the lists to Unknown | Nothing |
| Seats | Where each seat is held, from the seat partition;  sessions and listener state are the Mac's only, and a seat the server holds says Not read | The Mac listener's `~/.agent-sync/listener/status.json` (believed only when it says it is the Mac's instance and is under 60 seconds old), its configuration file, the seat partition, and `simple-chat users` and `search` | On open and on Refresh, and every five minutes while Muster is in front;  a reading under a minute old is reused, and one read runs at a time | Nothing:  `ChatCLI` refuses any verb but those two |
| Drift History | The Mac | `~/Library/Application Support/Muster/drift-history.json`, which the drift monitor writes when a cell turns red | As each turn happens | Clear History empties the list on the Mac |
| Services | The Mac | pm2 (through `node`), `/bin/launchctl`, the BotFleet MCP Server's own health endpoint and BotFleet Server's own local API, read live | Every two minutes, on open and on Refresh | pm2 rows and allowlisted LaunchAgents:  Start, Stop and Restart through `fleet-ops-control.py` on the Mac, plan and Touch ID.  BotFleet Server and any LaunchAgent the allowlist does not name:  the direct route (`launchctl`, and BotFleet Server's own API for its Stop), with no plan, no Touch ID and no audit row.  BotFleet Server's Start At Mac Login, and Launch Muster At Login |
| Wake & Listener | The Mac's listener | `~/.agent-sync/listener.toml`, the listener's `status.json` and `pause.json`, wake pins and `wakes.jsonl`, and `simple-chat daemon status --json` | On open, on Refresh, every minute while the panel is open, and when the app comes back to the front;  a read does not start while another action runs and never drops an unapplied edit | `listener.toml`, written only after `simple-chat daemon check-config` accepts the candidate, then `daemon reload`;  pause, resume, test wake and pin through `simple-chat daemon`, each after a confirmation.  Only seats the partition gives the Mac are changed;  a seat whose holder is not known keeps only its pause |
| Controls | Across the fleet | The knob registry in `INFISICAL.md`, the engine's own live read of Infisical (`python3 -m fleet_knobs status --json --values`), and the Mac's last-known-good copy `~/.local/state/fleet-knobs/lkg.json`;  secret names and the doctor's booleans from `fleet-secrets names` and `fleet-secrets doctor --json`;  Recall Settings from the recall service | On open and on Refresh, which both ask the engine for the live read;  a failed read keeps the last reading on screen with its own time and says why, and a copy older than an hour is stale and one older than 48 hours is not a reading | A non-secret knob:  Show Plan runs `python3 -m fleet_knobs set` and Reset To Default runs `unset`, on the Mac, plan and Touch ID, changing a value every machine reads.  Show Plan is off when a live read says Infisical already holds the value, and Reset To Default when it says Infisical holds none.  When the engine says it has no write token, both are off and the panel says changes cannot be made from Muster.  A secret is never edited here |
| Seat Setup | The Mac | One seat at a time, for any platform:  the engine's answer for each of the six config kinds (instructions, hooks, MCP, skills, sub-agents and permissions), the rules behind it, and the file each rule is about as the manifest names it, read on the Mac.  Below them, What Is Installed lists the platform's MCP servers, hooks, plugins, skills, sub-agents and commands as names and counts only, from `scripts/fleet_seat_inventory.py` (one implementation for both hosts, run on the Mac with `/usr/bin/python3 -I` from the trusted checkout, with a source list of its own for each platform).  MiniMax's seat shows `~/.minimax` instead (allowlisted `config.yaml` keys, plugins, hooks, channels and agents).  A file or folder that is absent or cannot be read says so and never reads as empty, and no value from a config file is ever read out | Each engine run;  the files are read again when the seat changes or a run finishes | Nothing |
| Server Status | The server | `~/.local/state/fleet-tools/server-status.json`:  the server's `fleet-server-status.timer` writes it every 15 minutes, and the Mac's `com.jay.fleet-tools-doctor` copies it every 15 minutes;  the panel reads the copy the menu reads | On open, after an action and on Read The Mac's Copy Again;  a report over 45 minutes old is not current, and the panel then lists the last known units | When the menu calls the report current:  units the report marks controllable get Start, Stop, Restart and Logs, and a Coolify application named by its label gets Redeploy.  When the report stopped, only the report timer gets Restart and Logs, as the menu offers.  All over `fleet-ops-ctl`, plan and Touch ID |
| Resource Limits | The server | `server resource-status coding` over `fleet-ops-ctl`:  a read-only plan, with no Touch ID | On open and on Refresh;  the receipt's age shows in seconds | One coding resource field per review:  `server resource-policy`, checked against the revision read, plan and Touch ID |
| Tools And Worker Health | The Mac and the server | `~/.local/state/fleet-tools/report.json` (`com.jay.fleet-tools-doctor`, every 15 minutes, reaching the server over the read-only `fleet-tools-ro` alias) and `worker-health.json` (`com.jay.fleet-worker-health`, every 15 minutes) | On open and on Refresh;  a report over 45 minutes old reads as unknown | Nothing |
| Virtual Machines | Each fleet VM | `~/.local/state/fleet-tools/vms/*.json`, pulled by `fleet-tools-doctor.py --vms`;  the doctor's LaunchAgent in this repository does not pass `--vms`, so nothing here schedules the pull | On open and on Refresh;  a report over 45 minutes old reads as unknown | Nothing, by the owner's call |
| Background Agents | The Mac | `mac-services.json` (`com.jay.fleet-mac-services`, every five minutes, or a live read on Read The Mac's Agents), the allowlist, and Hog Hunter's `hoghunter-mac.json` | On open and on Read The Mac's Agents;  the agents' file is current for 15 minutes and Hog Hunter's for five | LaunchAgents:  Start, Stop and Restart on the Mac, which the helper refuses for an agent the allowlist does not name, plan and Touch ID |
| Server Updates | The server | The allowlist's `server.updates`, read on the Mac | On open and on Read The Allowlist Again | Each update (`server update NAME`) over `fleet-ops-ctl`, plan and Touch ID |
| Action History | The Mac and the server | The last 50 rows of the Mac's `ops-audit.jsonl` | On open, after each action and on Read The Log Again | Nothing |

Settings holds the repository root and the scripts directory Muster runs from, the Zulip seat Seats reads as, Keep Services Running, the theme and Short Menu Bar Text.

Muster stores no credential.  It runs `/usr/bin/python3` for the engine, `fleet-view`, `fleet-ops-control.py` and `fleet_knobs`;  `simple-chat` for Seats and Wake & Listener;  `fleet-secrets` for Controls;  pm2 through `node`, and `/bin/launchctl`, for the Services panel's reads and its direct route;  and `/usr/bin/ssh` to `fleet-ops-ctl` for the server.  Two places do read a credential, by its key name through `fleet-secrets get`, for one request, and never show or store it:  Controls' Recall Settings, for the admin token whose key name it is given, and a BotFleet host configured with an auth token.

## What PASS Means

PASS means every check the rule asks for was proven, and nothing more.  The Overview's verdict line says what its own word means once the engine has answered:  PASS is every check every rule asks for proven, FAIL is at least one check failed or could not be completed, and UNKNOWN is nothing failed but not every check was proven.  A cell in Compliance Grid shows the proof it reached, never the proof its rules required, and a dash means none was reached;  `1/2` is partly proven, with the tooltip naming the rule that was not, and `NO GUARD` means the seat has no guard, so none was checked and none was proven.  A hook rule says which guards it asks to cover.  A badge in another panel says in its tooltip what it checked:  an app row in Apps & Work, that no lane is dirty, no pull request is failing or blocked and no claim has expired;  a host in Tools And Worker Health, that the doctor's report is current, the host answered and none of its tool rows failed, are behind or could not be read.  Any other PASS covers that panel's own check on that row and nothing else.  A Mac with a cell not yet proven is never shown as OK, and the menu counts those cells beside the red and drifted ones.

## Keeping Services Running

While Muster runs, it starts a service on the Mac again when it is found stopped or failed, unless Keep Services Running in Settings is turned off.  Each service has one of three policies, and its row in the Services panel says which:  Keep Running starts it again when it stops, waiting longer after every try (60 seconds, then doubling) and giving up after 3, with the count starting again once it has stayed up for ten minutes;  Start Once Per Launch starts it once, at the first look after Muster starts;  Leave Alone never starts it, which is the policy of BotFleet Server, its companion and the BotFleet MCP Server, and of every LaunchAgent that is only on the allowlist.  Muster only starts a service:  it never stops or restarts one, and it never starts one the owner stopped from Muster.  Those stops are kept in `~/Library/Application Support/Muster/owner-stops.json`, service ids and times only, so quitting Muster does not forget them, and while that file cannot be read nothing is started and the Services panel says so.  An automatic start takes the same route as the owner's own Start, with the same helper, plan and audit row, but with no Touch ID, because nobody is at a sheet.  What it did is said on the service's row, in the menu and in the Services panel.

## The Menu Bar

The menu bar item names both places:  `Mac OK · Server OK`, `Mac 2 · Server OK` for two things needing attention, `Mac 2+` when something else could not be read as well, and `?` for a place not read inside its window, or one with nothing wrong and some part not read, which includes a seat cell not yet proven.  Short Menu Bar Text in Settings shortens it to `M OK · S ?`.

The menu has a The Mac section and a The Server section, and everything sits under the place it runs on.  Opening it reads memory and never contacts the server.  Ask The Server Now is the one thing that does, only on a click, and an answer never turns the bar to OK, because only a current report does that.

Under The Mac, the menu says when the Mac was last checked and counts its services.  A service that failed or is not known moves to the top.  Seat Setup lists red and drifted cells and the cells not yet proven, each opening Compliance Grid.  Services On The Mac holds every service with the Start, Restart and Stop its state allows, and Check The Mac Again reads everything again.  Under The Server, it says when the server wrote its report and when the Mac copied it, counts timers and apps, and gives the fullest disk, memory and any high load.  Timers On The Server offers Restart or Start, and Read Logs, from the allowlist;  Stop stays in the Server Status panel, because stopping the report timer would blind this section.  When the report is not current the section says why, and it offers Fetch From The Server Again… (a plan to restart the Mac's doctor agent) where a new fetch could help.

The count beside a place adds up what needs attention there.  On the Mac:  red and drifted cells, services that failed or are not known, and a stopped fetch.  On the server:  units that are not active, apps that exited or are unhealthy, each disk and the memory at 85% or more, and a load of two per cpu or more.  A part that was not read is never counted as zero.

An action that ends in … opens a sheet or an alert first.  pm2 rows, allowlisted LaunchAgents and everything on the server go through the plan and Touch ID, from the menu and the window alike, apart from the automatic start under Keeping Services Running.  BotFleet Server and any LaunchAgent the allowlist does not name stay on the direct route, with no plan, no Touch ID and no audit row.  Their Stop asks first, and BotFleet Server's Restart asks first only when a fresh read finds work in flight.

## The Window

The sidebar opens on Overview, then three sections:  The Mac (Services, Background Agents, Compliance Grid, Drift History, Wake & Listener, Seat Setup), The Server (Server Status, Resource Limits, Server Updates) and Across The Fleet (Apps & Work, Seats, Controls, Tools And Worker Health, Virtual Machines, Action History).  Each panel's section comes from its place in `MusterDestination` and from nothing else, so a heading cannot hold a panel from another machine.  The old Operations panel is split into Background Agents, Server Updates and Action History, and a saved Operations place opens Server Updates.

Every panel has one name, the same in the sidebar, the window title and its own heading.  A strip over each panel shows one place badge, On The Mac, On The Server or Across The Fleet, and the line that says where its data came from.  The Overview's The Mac and The Server cards and Server Status's state are built from the menu's own summary of each place, in the same words, so the window and the menu cannot disagree.  Server Status shows the report's sections and their buttons only when the menu calls the report current.  When the server was reached and its report stopped, it lists the last known units with the allowlist's Restart and Logs, and when the Mac's fetch stopped or could not reach the server, it lists them with no repairs, because the menu offers a fetch instead.

## Scope

- `Muster/` holds the app:  every panel in the table under Every Panel, the menu bar item, Settings, and the models behind them, which `MusterTests/` covers without a view.
- `project.yml` is the XcodeGen source of truth.  The generated Xcode project is ignored.
- Merged changes reach the owner's Mac through Sparkle 2 from releases `.github/workflows/muster-release.yml` signs, notarizes and staples on each merge to main.  `docs/AUTO-UPDATE.md` describes the route, the feed, the gate that holds an install while an action runs, and what is still missing.
- `script/build_and_run.sh` is the developer route for trying an unmerged lane:  local project generation, signing, installation, and launch.
- The repository's `.codex/environments/environment.toml` exposes the same script through the Codex **Run** action.

Muster has no sample fleet records and no inferred health state:  a source that cannot be read shows as unknown, never as a pass or a zero.  What it runs, and the two places it reads a credential, are under Every Panel.

The Tools And Worker Health panel reads two local files and runs nothing:  the doctor's own report, which `com.jay.fleet-tools-doctor` writes every fifteen minutes and which covers the Mac and the server together, and the Worker Health report.  A report that is missing, malformed, of another version, or older than 45 minutes reads as unknown, and a host the doctor could not reach reads as unknown rather than as a failure.

## Server Status

The Server Status panel (AFC-548) reads one local file:  the server's own collector report, `~/.local/state/fleet-tools/server-status.json`, which `scripts/fleet-server-status.py` writes on the server and the Mac's doctor copies to the Mac.  It shows the host's load, memory and disks, the units the allowlist names or watches, the Coolify resources and their state, the busiest processes on the host, and the watched config files whose drift is not `unchanged`.  Its Start, Stop, Restart, Logs and Redeploy buttons run nothing on their own:  each one plans its action on the server over `fleet-ops-ctl` and asks Touch ID first, as Every Panel describes.

The collector is read only apart from that one report file, and it reads no credential:  Coolify comes from Docker's own labels and container stats, a process is identified by its executable and never by its argv or its environment, and a path that looks like a credential is never hashed and never listed.  A measurement that cannot be taken is left out of the report rather than reported as a zero, and a file that is gone reads as `missing` rather than as a zero byte file.  A report that is missing, malformed, of another version, or older than 45 minutes reads as unknown, and an empty half of the report reads as unproven rather than as a pass.

## Seats

The Seats panel (AFC-395) shows one row per seat from the app's own roster, grouped by the machine whose listener holds the seat in the seat partition (`docs/protocols/agent-sync-partition.toml`, or the copy the Mac's listener names):  Held By The Mac, Held By The Server, Held By No Eavesdropper.  Each row shows the live sessions the Mac's listener holds for the seat (with each session's letter, title, platform and verified machine), the listener's state for it with any running wake and its last event, the provider quota the listener reads for it (the same reading its wake gate uses), and its last Zulip post.  The panel leads with a sentence that says how many Mac seats are connected and working, and every column names its source and its time on screen.

Muster reads only the Mac's listener.  A seat the server holds says Not read in its listener columns, in plain text, because that is where the seat lives and not a fault, and sessions shown are on the Mac only.  The server's listener status is not copied to the Mac yet.

Sources:  the listener's own published state at `~/.agent-sync/listener/status.json` (believed only when it says it is the Mac's instance and was written in the last 60 seconds), its configuration file, the seat partition, and the fleet chat CLI (`simple-chat`).  The panel reads the CLI's `users` and `search` verbs and nothing else;  `ChatCLI` refuses any other verb before a process exists,  so the panel cannot write to Zulip.  It no longer asks for Zulip presence, because Zulip refuses presence for bot users.  It never posts as Jay:  the CLI resolves the credential of the bot seat named by `MUSTER_ZULIP_SEAT` (default `CLAUDE`),  and the app never sees a key.

A refresh makes at most one `users` call and one `search` call per seat, so the ceiling is `1 + seats`, and nothing for an empty roster;  the counts are under Details.  The reading is held for the whole app, so returning to the panel shows the last reading and reads again only when it is more than a minute old, one read runs at a time, and the five-minute timer reads only while Muster is in front.  A source that fails leaves its column Unknown with the reason under the row:  a missing binary, a refused call, an unreadable file and a listener state file more than 60 seconds old all read as unknown, never as "no sessions".  An empty roster says Muster has no seat list instead of showing zeros.

## Drift Alerts

The app watches the paths the manifest names and says when a cell turns red.  It is idle between refreshes:  FSEvents is the only thing that wakes it,  and there is no poll of any file's contents.

The watch list is **derived from the manifest**,  never written down:  every rule's `check.target` becomes a path,  `~` is expanded,  and a rule that checks a credential is skipped because the app never reads a credential.  A new seat is watched because the manifest names its paths.  Each path is watched by its own path,  which FSEvents filters to that path alone;  a path that is not there yet is watched too,  and FSEvents reports it when it appears.  The nearest directory above it is watched as well,  unless that directory is the home directory:  FSEvents watches a directory recursively,  and the home directory is the largest tree on the Mac.

A change is gathered for a second and a half,  so a write that lands in two pieces is one run,  and the engine runs again.  A slow safety net runs the engine every ten minutes for a change the watcher could not see.  A cell that was not red and becomes red posts one notification that names the seat and the check;  a cell that stays red says nothing again,  and a cell that recovers and turns red again says so again.  The notification fails soft:  an app without permission,  or a process without a bundle,  records the turn and posts nothing.  The menu bar item counts red and drifted cells among the things needing attention on the Mac, in plain words, with no colour of its own.

The first run that could be trusted is a reading and not a change:  there is no earlier answer to compare it with.  A run that could not be trusted says nothing,  and the count is unknown rather than zero.

## App Icon

The Dock icon follows the mode: dark artwork in Dark, light artwork in Light.  The Theme in Settings decides, and when it is System the icon follows macOS, changing as macOS changes.  `AppIconFollower` does this while the app runs.

macOS shows the icon in the app bundle whenever Muster is not running, in Finder and in a Dock tile, and that one is always the light artwork.

The owner's artwork is in `icon-source/`.  After changing it, regenerate `Muster/Assets.xcassets` with `python3 muster/script/generate_app_icons.py`, which needs Pillow.  `script/test_app_icons.py` checks the result, and the macOS CI job checks the built bundle.

## Prerequisites

- macOS 14 or later.
- Full Xcode, selected with `xcode-select`, including its macOS SDK and command-line tools.
- XcodeGen 2.38 or later (`brew install xcodegen`).
- Python 3 for public signing-identity selection.
- A valid **Developer ID Application** identity and its usable private key in the local keychain.  The script never exports private key material.

The script requires exactly one matching valid Developer ID Application identity.  When several exist, set `MUSTER_SIGNING_IDENTITY` to the exact certificate name or SHA-1 fingerprint to select one; it fails for a missing or ambiguous match.  Team and certificate identifiers are discovered locally, not committed.

## Build And Run

From the repository root:

```sh
./muster/script/build_and_run.sh --verify
```

This is the developer route, for a lane on a Mac.  The owner's copy is delivered by the release workflow, never by this script.

The build number is the 12-digit `YYYYMMDDHHMM` time of the commit being built, in UTC, read from git.  The script also stamps the commit, its time and a channel into the Info.plist:  `main` for a clean tree whose commit is on `origin/main` as last fetched, `lane` otherwise (Settings and the menu then say Not From Main), and `development` for `--development`.  `MUSTER_BUILD_NUMBER` overrides the number, and a malformed override refuses before anything runs.  A build numbered below the installed copy is refused unless `--allow-older` is given, because Sparkle would then offer the newer release again.  Marketing version and signing settings remain those in the project.

The default Release configuration builds `services.jays.Muster` with Developer ID signing and hardened runtime enabled.  App Sandbox is disabled to leave room for the separately owned CLI integration; the app currently has no capabilities or custom entitlements.

The script installs the sole normal app copy at `~/Applications/Muster.app`, launches that bundle, and verifies its exact executable path with `--verify`.  Its `dist/` staging directory is removed when the script exits, including on failure.  Installation prepares and verifies a copy on the destination filesystem before replacing the installed bundle, and restores the previous bundle if replacement or verification fails.  Incremental build data stays under ignored `muster/.build/`.

If Muster is already running, the script stops before building.  Quit it manually, or explicitly authorize this invocation to terminate only the installed copy:

```sh
./muster/script/build_and_run.sh --verify --relaunch
```

The script never force-kills a process.  A second concurrent invocation fails against a local lock; after an interrupted process, inspect whether a build is still active before removing `muster/.build/run.lock`.

Optional modes are `--logs`, `--telemetry`, and `--debug`.  Log modes stream unified logs after launch;  `--telemetry` shows Muster's own subsystem, whose one category today is `drift`, from the drift monitor.  The build lock and staging are released before a log stream or debugger blocks.  Debug mode attaches LLDB to the installed process and is subject to macOS Developer Tools permissions and hardened-runtime debugger restrictions.

Full local builds follow the repository build coordination protocol.  The script is an on-demand developer helper, not an always-on service.

## Development Identity

```sh
./muster/script/build_and_run.sh --development --verify
```

Debug uses `services.jays.Muster.dev`, a distinct development bundle ID.  Both configurations deliberately install to the same `~/Applications/Muster.app` path so they cannot leave multiple normal app copies.  Run the default Release command after quitting a development build to replace it with the stable identity.  A Debug build carries no update feed, so it never updates itself;  a Release lane build does, and the next release from main with a larger build number replaces it.

These bundle IDs are working choices pending product identity onboarding.  They are not a finalized fleet registry, Apple portal, or distribution registration.

## Verify Locally

Project generation and shell validation do not compile the app:

```sh
bash -n muster/script/build_and_run.sh
xcodegen generate --spec muster/project.yml
```

The install transaction's synthetic tests touch no keychain, app or process, so they run anywhere:

```sh
python3 -m unittest discover -s muster/script -t muster/script -v
```

After a successful `--verify` run, independently inspect the installed signature and bundle metadata:

```sh
codesign --verify --strict --deep ~/Applications/Muster.app
codesign -d --verbose=4 ~/Applications/Muster.app
/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' ~/Applications/Muster.app/Contents/Info.plist
test ! -d muster/dist
```

Expect a Developer ID Application authority and the `runtime` signing flag.  The owner does no manual UI checks:  native Mac UI is verified through code review and the CI job named Muster macOS build, which builds the app unsigned, checks the built bundle's icons and runs the unit tests.  Process verification alone does not prove rendered appearance.

## Apply Preview

`FleetConfigCLI.swift` is the one wrapper around the engine:  `/usr/bin/python3 -m fleet_config <verb>` run from the repository's `scripts/` directory, arguments as an argv array, output decoded tolerantly.  Any view can call a verb through `FleetConfigRunning`.  `ApplyCoordinator.shared.beginPlan(seat:ruleID:kind:)` is the entry point for a red cell:  it runs `apply SEAT KIND --plan`, shows the planned writes in `ApplyPlanView`, asks once, then runs `apply SEAT KIND --yes` in the background and re-proves with `status --json --seat SEAT --kind KIND --no-doctor`.  The apply is owned by the app-lifetime coordinator, not the view, so closing the sheet or window does not cancel it, and a notification reports the result.  A refusal (Touch ID declined or unavailable, a manifest rule that is not auto-fixable, an engine refusal) leaves the cell red and is never retried.

The Touch ID gate for persistence-class writes lives inside the engine, behind a fixed-path signed helper.  Muster neither reimplements nor bypasses it, passes no helper path, and strips `FLEET_CONFIG_*` and `PYTHON*` variables from the child environment.  Muster has no URL scheme, AppleScript, App Intents or Shortcuts entry point, and `script/test_no_external_entry_points.py` keeps it that way.

The repository root is a setting:  the `fleetConfigRepoRoot` user default (`defaults write services.jays.Muster fleetConfigRepoRoot /path/to/repo`), then the `MUSTER_REPO_ROOT` environment variable, then `~/Code/AI-Fleet-Coordinator`.

Apply unit tests are an XcodeGen target that compiles the non-view sources directly, so the tests need no signing of their own.  The scheme still builds the Developer ID app target, so the command passes the same signing overrides as the CI job named Muster macOS build, and this is the command that job runs.  The same target decodes the server, Worker Health and LaunchAgent fixtures in `MusterTests/Fixtures/` (current, old shape, malformed and stale), drives the operations flow with an injected runner, so no test starts a process or asks Touch ID, and checks this README's Every Panel table against the app's places:

```sh
cd muster && xcodegen generate --spec project.yml
xcodebuild -project Muster.xcodeproj -scheme Muster -configuration Debug \
  -destination 'platform=macOS' -derivedDataPath .build/CITest \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY= test
```

## Hog Hunter On This Mac

The Background Agents panel shows what the Hog Hunter Mac app says about this Mac: load, CPU, memory, swap, each mounted volume, the busiest processes by name, the last cleanup, the housekeeper lock, the cleaning regimen, the age of the server snapshot Hog Hunter holds, and its version.  The source is one local file the running app writes about once a minute, `~/.local/state/fleet-tools/hoghunter-mac.json` (tool `hoghunter-mac-status`, schemaVersion 1; decoder `Muster/Model/HogHunterMacReport.swift`).  Muster only reads it and has no control of Hog Hunter: no command, no connection and no credential.  The file's authentication is the file system: Muster reads it only when it is a regular file (not a link), owned by the current user and not writable by group or others, and no larger than 256 KB.  A missing file or a report older than 5 minutes reads as unknown, and the panel says Hog Hunter is not running or has not reported.  A file that fails the ownership or link check, a wrong version or tool, or a malformed file reads as unknown too, but states its own reason instead.  Fields the app could not measure are absent and show as Not reported, never as zero.

## Delivery And Build Identity

Muster updates itself through Sparkle 2.  `.github/workflows/muster-release.yml` builds, signs, notarizes and staples each merge to main that changes `muster/`, and installed copies read Muster's own feed, the fixed `muster-feed` release, once an hour.  An automatic install waits until no apply, operations run or service action is in flight and Muster is no longer the app in front.  An Install And Relaunch the owner chooses waits only for work in flight.  `docs/AUTO-UPDATE.md` has the whole route.

Settings ▸ This Build and the foot of the menu show the version, the build number and the short commit, with Development Build or Not From Main when that applies, and the commit of the checkout Muster runs its Python from, saying plainly when the two differ.

What is not done yet:  Muster's own Sparkle key does not exist, and the release's read tokens may not exist in this repository, so the workflow builds and verifies and publishes nothing until they do (it names what is missing).  The copy on the owner's Mac predates Sparkle and needs one Sparkle-enabled install by a Mac seat.  Both are in `docs/AUTO-UPDATE.md`.

Muster currently lives inside an existing application repository.  The current onboarding flow does not directly fit this nested product; use the existing product registry rather than creating duplicate infrastructure.  Product identity, registration applicability, and future onboarding remain pending.  Only authorized maintainers may perform that setup.
