# Muster

Muster is a native SwiftUI macOS 14+ scaffold.  Its initial window explicitly reports unavailable fleet status because no data source is connected.

## Scope

- `Muster/` contains the app entry point, the fleet overview view, the Fleet View (AFC-394:  lanes, open PRs and Linear claims per seat, from `scripts/fleet_view/`), the Fleet Hosts panel (what the fleet tools doctor's own report says about each host, from `~/.local/state/fleet-tools/report.json`) and the Drift History panel (AFC-390:  every cell that turned red,  kept in `~/Library/Application Support/Muster/drift-history.json`).
- `project.yml` is the XcodeGen source of truth.  The generated Xcode project is ignored.
- `script/build_and_run.sh` owns local project generation, signing, installation, and launch.
- The repository's `.codex/environments/environment.toml` exposes the same script through the Codex **Run** action.

The app makes no network calls and reads no credentials.  It has no sample fleet records or inferred health state.  The only CLI it runs is `fleet-config` (see Apply Preview below), and only when asked;  collector and grid integration remain separate work.

The Fleet Hosts panel reads one local file and runs nothing:  the doctor's own report, which `com.jay.fleet-tools-doctor` writes every fifteen minutes and which covers the Mac and the server together.  A report that is missing, malformed, of another version, or older than 45 minutes reads as unknown, and a host the doctor could not reach reads as unknown rather than as a failure.

## Drift Alerts

The app watches the paths the manifest names and says when a cell turns red.  It is idle between refreshes:  FSEvents is the only thing that wakes it,  and there is no poll of any file's contents.

The watch list is **derived from the manifest**,  never written down:  every rule's `check.target` becomes a path,  `~` is expanded,  and a rule that checks a credential is skipped because the app never reads a credential.  A new seat is watched because the manifest names its paths.  Each path is watched by its own path,  which FSEvents filters to that path alone;  a path that is not there yet is watched too,  and FSEvents reports it when it appears.  The nearest directory above it is watched as well,  unless that directory is the home directory:  FSEvents watches a directory recursively,  and the home directory is the largest tree on the Mac.

A change is gathered for a second and a half,  so a write that lands in two pieces is one run,  and the engine runs again.  A slow safety net runs the engine every ten minutes for a change the watcher could not see.  A cell that was not red and becomes red posts one notification that names the seat and the check;  a cell that stays red says nothing again,  and a cell that recovers and turns red again says so again.  The notification fails soft:  an app without permission,  or a process without a bundle,  records the turn and posts nothing.  The menubar item carries the red count,  in the same red the grid uses.

The first run that could be trusted is a reading and not a change:  there is no earlier answer to compare it with.  A run that could not be trusted says nothing,  and the count is unknown rather than zero.

## Prerequisites

- macOS 14 or later.
- Full Xcode, selected with `xcode-select`, including its macOS SDK and command-line tools.
- XcodeGen 2.38 or later (`brew install xcodegen`).
- Python 3 for public signing-identity selection.
- A valid **Developer ID Application** identity and its usable private key in the local keychain.  The script never exports private key material.

The script requires exactly one matching valid Developer ID Application identity.  When several exist, set `MUSTER_SIGNING_IDENTITY` to the exact certificate name or SHA-1 fingerprint to select one; it fails for a missing or ambiguous match.  Team and certificate identifiers are discovered locally, not committed.

## Build And Run

From the repository root:

```sh
./muster/script/build_and_run.sh --verify
```

The default Release configuration builds `services.jays.Muster` with Developer ID signing and hardened runtime enabled.  App Sandbox is disabled to leave room for the separately owned CLI integration; the app currently has no capabilities or custom entitlements.

The script installs the sole normal app copy at `~/Applications/Muster.app`, launches that bundle, and verifies its exact executable path with `--verify`.  Its `dist/` staging directory is removed when the script exits, including on failure.  Installation prepares and verifies a copy on the destination filesystem before replacing the installed bundle, and restores the previous bundle if replacement or verification fails.  Incremental build data stays under ignored `muster/.build/`.

If Muster is already running, the script stops before building.  Quit it manually, or explicitly authorize this invocation to terminate only the installed copy:

```sh
./muster/script/build_and_run.sh --verify --relaunch
```

The script never force-kills a process.  A second concurrent invocation fails against a local lock; after an interrupted process, inspect whether a build is still active before removing `muster/.build/run.lock`.

Optional modes are `--logs`, `--telemetry`, and `--debug`.  Log modes stream unified logs after launch; the scaffold has no custom telemetry events yet.  The build lock and staging are released before a log stream or debugger blocks.  Debug mode attaches LLDB to the installed process and is subject to macOS Developer Tools permissions and hardened-runtime debugger restrictions.

Full local builds follow the repository build coordination protocol.  The script is an on-demand developer helper, not an always-on service.

## Development Identity

```sh
./muster/script/build_and_run.sh --development --verify
```

Debug uses `services.jays.Muster.dev`, a distinct development bundle ID.  Both configurations deliberately install to the same `~/Applications/Muster.app` path so they cannot leave multiple normal app copies.  Run the default Release command after quitting a development build to replace it with the stable identity.

These bundle IDs are scaffold choices pending product identity onboarding.  They are not a finalized fleet registry, Apple portal, or distribution registration.

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

Expect a Developer ID Application authority and the `runtime` signing flag.  Inspect the window for the light appearance and unavailable fleet status.  Native Mac UI verification follows repository policy through code review and CI; process verification alone does not prove rendered appearance.

## Apply Preview

`FleetConfigCLI.swift` is the one wrapper around the engine:  `/usr/bin/python3 -m fleet_config <verb>` run from the repository's `scripts/` directory, arguments as an argv array, output decoded tolerantly.  Any view can call a verb through `FleetConfigRunning`.  `ApplyCoordinator.shared.beginPlan(seat:ruleID:kind:)` is the entry point for a red cell:  it runs `apply SEAT KIND --plan`, shows the planned writes in `ApplyPlanView`, asks once, then runs `apply SEAT KIND --yes` in the background and re-proves with `status --json --seat SEAT --kind KIND --no-doctor`.  The apply is owned by the app-lifetime coordinator, not the view, so closing the sheet or window does not cancel it, and a notification reports the result.  A refusal (Touch ID declined or unavailable, a manifest rule that is not auto-fixable, an engine refusal) leaves the cell red and is never retried.

The Touch ID gate for persistence-class writes lives inside the engine, behind a fixed-path signed helper.  Muster neither reimplements nor bypasses it, passes no helper path, and strips `FLEET_CONFIG_*` and `PYTHON*` variables from the child environment.  Muster has no URL scheme, AppleScript, App Intents or Shortcuts entry point, and `script/test_no_external_entry_points.py` keeps it that way.

The repository root is a setting:  the `fleetConfigRepoRoot` user default (`defaults write services.jays.Muster fleetConfigRepoRoot /path/to/repo`), then the `MUSTER_REPO_ROOT` environment variable, then `~/Code/AI-Fleet-Coordinator`.

Apply unit tests are an XcodeGen target that compiles the non-view sources directly, so they run unsigned:

```sh
cd muster && xcodegen generate --spec project.yml
xcodebuild -project Muster.xcodeproj -scheme Muster -destination 'platform=macOS' test
```

## Delivery And Onboarding Limits

Local Developer ID signing is established from the first build.  This scaffold does not implement notarization, stapling, release packaging, an updater, hosted CI signing, or a public release.  A locally verified signature is not evidence of notarization or completed product onboarding.

Muster currently lives inside an existing application repository.  The current onboarding flow does not directly fit this nested product; use the existing product registry rather than creating duplicate infrastructure.  Product identity, registration applicability, and future onboarding remain pending.  Only authorized maintainers may perform that setup.
